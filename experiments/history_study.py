"""過去2年分の大量のデータで、デイトレードで勝てるパターンを探す。

  python3 experiments/history_study.py lists     開示一覧を集める（1日1回の取得、約15分）
  python3 experiments/history_study.py prices    株価（日足）を集める
  python3 experiments/history_study.py analyze   パターンを調べる

調べ方:
  - 出来事 = その日の寄り付き前（前の営業日 15:25 〜 当日 8:59）に出た開示、または前日の大きな値動き
  - 結果 = その日の「始値で買って終値で売った」損益率（コスト往復0.1%を引く）
  - 前半（学習用）で見つけたパターンが、後半（確認用）でも通用するかを確かめる。
    両方で、件数が十分あり、平均がプラスのものだけを「使えそうなパターン」とする
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
H = ROOT / "experiments" / "history"
START, END = dt.date(2024, 11, 1), dt.date(2026, 11, 25)
SPLIT = dt.date(2025, 11, 1)     # これより前が学習用、以降が確認用
COST = 0.001
MIN_TURNOVER = 3e7
MIN_PRICE = 200

# 開示の種類（上から順に判定。1つの開示は最初に当てはまった種類になる）
KINDS = [
    ("TOB・MBO", ("公開買付", "ＭＢＯ", "MBO")),
    ("上方修正", ("上方修正",)),
    ("下方修正", ("下方修正",)),
    ("業績予想の修正", ("業績予想の修正", "業績予想修正", "通期業績予想")),
    ("増配・復配", ("増配", "復配", "記念配当", "特別配当")),
    ("減配・無配", ("減配", "無配")),
    ("配当予想の修正", ("配当予想の修正", "配当予想修正")),
    ("自社株買い", ("自己株式の取得に係る事項の決定", "自己株式取得に係る事項の決定", "自己株式の取得に関するお知らせ")),
    ("株式分割", ("株式分割",)),
    ("株主優待", ("株主優待",)),
    ("提携・M&A", ("資本業務提携", "業務提携", "子会社化", "株式取得", "買収")),
    ("受注・契約", ("受注", "契約締結")),
    ("承認・治験", ("承認", "治験", "臨床試験")),
    ("決算短信", ("決算短信",)),
]
NOISE = ("訂正", "取得状況", "取得結果", "取得終了", "消却", "説明資料", "説明会")


def _get(url):
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "daytrade-bot research"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            time.sleep(3 * (i + 1))
    raise RuntimeError(url)


def lists():
    out = H / "lists"
    out.mkdir(parents=True, exist_ok=True)
    d = START - dt.timedelta(days=5)
    n = 0
    while d <= END:
        p = out / f"{d:%Y%m%d}.json"
        if not p.exists():
            raw = json.loads(_get(f"https://webapi.yanoshin.jp/webapi/tdnet/list/{d:%Y%m%d}.json?limit=10000"))
            items = [x.get("Tdnet", x) for x in raw.get("items", [])]
            p.write_text(json.dumps([{k: x[k] for k in ("pubdate", "company_code", "company_name", "title")}
                                     for x in items], ensure_ascii=False))
            n += 1
            time.sleep(1)
            if n % 50 == 0:
                print(f"  {d} まで取得（{n}日分）", flush=True)
        d += dt.timedelta(days=1)
    print("開示一覧: 完了")


def _kind(title: str) -> str | None:
    if any(w in title for w in NOISE):
        return None
    for k, words in KINDS:
        if any(w in title for w in words):
            return k
    return None


def events() -> pd.DataFrame:
    """取引日ごとの、寄り付き前の開示（会社ごとに一番強い種類を1つ）。"""
    import jpholiday
    def trading(d):
        return d.weekday() < 5 and not jpholiday.is_holiday(d) and not (
            (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3))
    rows = []
    for f in sorted((H / "lists").glob("*.json")):
        for x in json.loads(f.read_text()):
            k = _kind(x["title"])
            if k:
                rows.append({"t": dt.datetime.fromisoformat(x["pubdate"]), "code": x["company_code"][:4],
                             "name": x["company_name"], "kind": k, "title": x["title"]})
    disc = pd.DataFrame(rows)
    # 開示の時刻 → その開示を寄り付き前に読める取引日
    def target(t: dt.datetime) -> dt.date:
        d = t.date()
        if t.time() > dt.time(15, 25):
            d += dt.timedelta(days=1)
        elif t.time() > dt.time(8, 59):
            return None   # 取引時間中の開示は対象外
        while not trading(d):
            d += dt.timedelta(days=1)
        return d
    disc["date"] = disc["t"].map(target)
    disc = disc.dropna(subset=["date"])
    disc = disc[(disc["date"] >= START) & (disc["date"] <= END)]
    order = {k: i for i, (k, _) in enumerate(KINDS)}
    disc["rank"] = disc["kind"].map(order)
    ev = disc.sort_values("rank").groupby(["date", "code"]).first().reset_index()
    return ev[["date", "code", "name", "kind", "title"]]


def prices():
    ev = events()
    codes = sorted(set(ev["code"]))
    uni = ROOT / "morning" / "movers_universe.tsv"
    if uni.exists():
        codes = sorted(set(codes) | {l.split("\t")[0] for l in uni.read_text().splitlines() if l and not l.startswith("#")})
    out = H / "prices"
    out.mkdir(parents=True, exist_ok=True)
    import yfinance as yf
    todo = [c for c in codes if not (out / f"{c}.csv").exists()]
    print(f"株価: {len(codes)}銘柄（未取得 {len(todo)}）", flush=True)
    for i in range(0, len(todo), 150):
        chunk = todo[i:i + 150]
        px = yf.download([f"{c}.T" for c in chunk], start=str(START - dt.timedelta(days=45)),
                         end=str(END + dt.timedelta(days=1)), interval="1d", progress=False,
                         auto_adjust=False, group_by="ticker", threads=True)
        for c in chunk:
            t = f"{c}.T"
            if t in px.columns.get_level_values(0):
                df = px[t].dropna(subset=["Open", "Close"])
                if len(df):
                    df[["Open", "High", "Low", "Close", "Volume"]].to_csv(out / f"{c}.csv")
                    continue
            (out / f"{c}.csv").write_text("Date,Open,High,Low,Close,Volume\n")
        print(f"  {min(i + 150, len(todo))}/{len(todo)}", flush=True)
    print("株価: 完了")


def _load_prices() -> dict[str, pd.DataFrame]:
    out = {}
    for f in (H / "prices").glob("*.csv"):
        df = pd.read_csv(f, index_col=0)
        if len(df) < 30:
            continue
        df.index = pd.to_datetime(df.index).date
        df = df[~pd.Index(df.index).duplicated()]
        df["prev_close"] = df["Close"].shift(1)
        df["ret_oc"] = df["Close"] / df["Open"] - 1 - COST
        df["gap"] = df["Open"] / df["prev_close"] - 1
        df["turnover20"] = (df["Close"] * df["Volume"]).shift(1).rolling(20).mean()
        df["vol20"] = df["Volume"].shift(1).rolling(20).mean()
        df["prev_ret"] = df["prev_close"] / df["Close"].shift(2) - 1
        df["prev_vr"] = df["Volume"].shift(1) / df["Volume"].shift(2).rolling(20).mean()
        out[f.stem] = df
    return out


def _table(df: pd.DataFrame, by: list[str], min_n: int = 30) -> pd.DataFrame:
    df = df.assign(half=np.where(df["date"] < SPLIT, "学習用", "確認用"))
    g = df.groupby(by + ["half"])["ret_oc"].agg(["count", "mean", "std", lambda s: (s > 0).mean()])
    g.columns = ["n", "mean", "std", "win"]
    t = g.unstack("half")
    t.columns = [f"{a}_{b}" for a, b in t.columns]
    t = t.dropna()
    t = t[(t["n_学習用"] >= min_n) & (t["n_確認用"] >= min_n)]
    t["t_学習用"] = t["mean_学習用"] / (t["std_学習用"] / np.sqrt(t["n_学習用"]))
    t["t_確認用"] = t["mean_確認用"] / (t["std_確認用"] / np.sqrt(t["n_確認用"]))
    return t.sort_values("mean_学習用", ascending=False)


def _fmt(t: pd.DataFrame, title: str) -> str:
    lines = [f"### {title}", "| 条件 | 学習用 件数 | 平均 | 勝率 | 確認用 件数 | 平均 | 勝率 | 判定 |", "|---|---|---|---|---|---|---|---|"]
    for idx, r in t.iterrows():
        name = " / ".join(map(str, idx)) if isinstance(idx, tuple) else str(idx)
        ok = r["mean_学習用"] > 0 and r["mean_確認用"] > 0 and r["t_学習用"] > 2
        lines.append(f"| {name} | {int(r['n_学習用'])} | {r['mean_学習用'] * 100:+.2f}% | {r['win_学習用']:.0%} | "
                     f"{int(r['n_確認用'])} | {r['mean_確認用'] * 100:+.2f}% | {r['win_確認用']:.0%} | {'◎ 使えそう' if ok else ''} |")
    return "\n".join(lines)


def analyze():
    px = _load_prices()
    ev = events()
    rows = []
    for r in ev.itertuples():
        p = px.get(r.code)
        if p is None or r.date not in p.index:
            continue
        x = p.loc[r.date]
        if pd.isna(x["ret_oc"]) or pd.isna(x["turnover20"]) or x["turnover20"] < MIN_TURNOVER or x["prev_close"] < MIN_PRICE:
            continue
        rows.append({"date": r.date, "code": r.code, "kind": r.kind, "gap": x["gap"], "ret_oc": x["ret_oc"]})
    E = pd.DataFrame(rows)
    gb = [-9, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10, 9]
    gl = ["-5%未満", "-5〜-2%", "-2〜0%", "0〜+2%", "+2〜+5%", "+5〜+10%", "+10%以上"]
    E["gap_b"] = pd.cut(E["gap"], gb, labels=gl)

    # 前日に大きく動いた銘柄（開示なし）
    disc_keys = set(zip(E["date"], E["code"]))
    M = []
    for code, p in px.items():
        q = p[(p.index >= START) & (p.index <= END)].dropna(subset=["ret_oc", "prev_ret", "prev_vr", "turnover20"])
        q = q[(q["turnover20"] >= MIN_TURNOVER) & (q["prev_close"] >= MIN_PRICE)]
        q = q[((q["prev_ret"].abs() >= 0.05) & (q["prev_vr"] >= 2)) | (q["prev_vr"] >= 4)]
        for d, x in q.iterrows():
            if (d, code) not in disc_keys:
                M.append({"date": d, "code": code, "prev_ret": x["prev_ret"], "prev_vr": x["prev_vr"],
                          "gap": x["gap"], "ret_oc": x["ret_oc"]})
    M = pd.DataFrame(M)
    M["prev_b"] = pd.cut(M["prev_ret"], [-9, -0.10, -0.05, 0.05, 0.10, 9],
                         labels=["前日-10%以下", "前日-10〜-5%", "前日±5%（出来高急増）", "前日+5〜+10%", "前日+10%以上"])
    M["gap_b"] = pd.cut(M["gap"], gb, labels=gl)

    out = [f"# 過去データでのパターン探し（{START}〜{END}）",
           f"学習用: 〜{SPLIT - dt.timedelta(days=1)} / 確認用: {SPLIT}〜。損益は始値で買い終値で売った場合（コスト込み）",
           f"開示の出来事 {len(E)}件、前日に動いた銘柄の出来事 {len(M)}件",
           "「◎ 使えそう」= 学習用・確認用の両方で平均がプラス、かつ学習用で偶然とは考えにくい（t>2）", ""]
    out.append(_fmt(_table(E, ["kind"]), "開示の種類別"))
    out.append("")
    out.append(_fmt(_table(E, ["kind", "gap_b"]), "開示の種類 × 寄り付きの窓"))
    out.append("")
    out.append(_fmt(_table(M, ["prev_b"]), "前日に動いた銘柄（開示なし）"))
    out.append("")
    out.append(_fmt(_table(M, ["prev_b", "gap_b"]), "前日の動き × 寄り付きの窓"))
    out.append("")
    base = pd.concat([E["ret_oc"], M["ret_oc"]])
    out.append(f"参考: 全体の平均 {base.mean() * 100:+.2f}%（{len(base)}件）")
    report = "\n".join(out)
    (ROOT / "experiments" / "results").mkdir(exist_ok=True)
    (ROOT / "experiments" / "results" / "history_study.md").write_text(report, encoding="utf-8")
    E.to_csv(H / "events.csv", index=False)
    M.to_csv(H / "movers.csv", index=False)
    print(report)


if __name__ == "__main__":
    {"lists": lists, "prices": prices, "analyze": analyze}[sys.argv[1]]()
