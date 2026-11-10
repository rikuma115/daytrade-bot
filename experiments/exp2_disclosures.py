"""実験2: 前日の引け後〜当日の朝に出た決算などの開示を Claude に読ませて、その日に買う銘柄を選ばせると勝てるか。

方法:
  - 対象期間: TDnet で書類（PDF）が読める直近約1か月（2026年10月〜11月上旬の決算シーズン）
  - 対象の開示: 決算短信、業績予想の修正、配当予想の修正、自己株式の取得（訂正は除く）
  - 時間帯: 前の営業日 15:25 より後 〜 当日 8:59 に出たもの（寄り付き前に読める開示）
  - 流動性: 過去20日の平均売買代金が5億円以上の銘柄だけ（デイトレードで売買できる銘柄）
  - Claude には寄り付き前に見える情報（開示の題名と本文の冒頭）だけを渡し、
    「当日の寄り付きで買い、大引け前に売る」デイトレードで買うべきかを判断させる
  - 成績は、日足の始値で買って終値で売った場合の損益率（コスト往復0.1%を引く）

事前に決めた合格基準（結果を見てから変えない）:
  1. Claude が「買う」と答えた銘柄の平均損益が、全候補の平均を上回る
  2. ランダムに同じ件数を選んだ場合と比べて、偶然では起きにくい（並べ替え検定で p < 0.10）
  3. 「買う」だけの成績が、コスト控除後でプラス
  1〜3を全て満たしたら「効果あり」、1と2だけなら「見込みあり（追加検証）」、それ以外は「効果なし」
  参考: Claude の評価（-2〜+2）と、当日の寄り付きの窓（前日終値→始値）の相関も出す。
  これは「開示の良し悪しを正しく読めているか」の確認で、売買の成績とは別。

  python3 experiments/exp2_disclosures.py --prepare        データ集め（開示、PDF、株価）
  python3 experiments/exp2_disclosures.py --model claude-haiku-5-5
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))
from exp1_signals import permutation_p  # noqa: E402

D = ROOT / "experiments" / "exp2_data"
OUT = ROOT / "experiments" / "results"
COST = 0.001
KINDS = ("決算短信", "業績予想", "業績修正", "配当予想", "自己株式の取得", "自己株式取得")

SYSTEM = """あなたは日本株のデイトレーダーを補助する。
前の営業日の引け後から今朝までに出た、ある会社の適時開示を読み、
「今日の寄り付きで買い、大引け前に売る」デイトレードで買うべきかを判断する。

注意:
- 良いニュースは寄り付きの時点で株価に織り込まれていることが多い（寄り付きで大きく上に窓を開ける）。
  寄り付き後もさらに上がりそうか（市場の予想を大きく上回る、一時的でない、材料が強い）を考える
- 売買のたびに往復で約 0.1% のコストがかかる
- 迷う場合は買わない

impact: 開示内容の株価への影響（-2: とても悪い 〜 0: 中立 〜 +2: とても良い）
take_long: 寄り付きで買うべきなら true
confidence: 0〜100
reason: 60字以内"""

SCHEMA = {
    "type": "object",
    "properties": {
        "impact": {"type": "integer"},
        "take_long": {"type": "boolean"},
        "confidence": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["impact", "take_long", "confidence", "reason"],
    "additionalProperties": False,
}


def _get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "daytrade-bot research"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def fetch_list(day: dt.date) -> list[dict]:
    p = D / "lists" / f"{day:%Y%m%d}.json"
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    # 保存済みの一覧を使うのは、確定した過去の日だけ。直近2日分は開示が増えていくので毎回取り直す
    if p.exists() and (today - day).days >= 2:
        return json.loads(p.read_text())
    raw = json.loads(_get(f"https://webapi.yanoshin.jp/webapi/tdnet/list/{day:%Y%m%d}.json?limit=10000"))
    items = [x.get("Tdnet", x) for x in raw.get("items", [])]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False))
    time.sleep(1)
    return items


def trading_days(start, end):
    import yfinance as yf
    idx = yf.download("1306.T", start=start, end=end, progress=False, auto_adjust=False).index
    return [i.date() for i in idx]


def prepare(start="2026-10-08", end="2026-11-07"):
    import yfinance as yf
    days = trading_days("2026-09-01", end)
    tdays = [d for d in days if d >= dt.date.fromisoformat(start)]
    # 1. 開示一覧（休日の分も含めて、各取引日の前日からの全ての日）
    cands = []
    for i, d in enumerate(tdays):
        prev = days[days.index(d) - 1]
        lo = dt.datetime.combine(prev, dt.time(15, 25))
        hi = dt.datetime.combine(d, dt.time(8, 59))
        day = prev
        while day <= d:
            for x in fetch_list(day):
                t = dt.datetime.fromisoformat(x["pubdate"])
                title = x["title"]
                if lo < t <= hi and any(k in title for k in KINDS) and "訂正" not in title:
                    code = x["company_code"][:4]
                    url = x["document_url"].split("rd.php?")[-1]
                    cands.append({"date": d, "code": code, "name": x["company_name"], "title": title,
                                  "pubdate": x["pubdate"], "url": url})
            day += dt.timedelta(days=1)
    c = pd.DataFrame(cands)
    print(f"開示 {len(c)}件 / 会社×日 {c.groupby(['date', 'code']).ngroups}件")
    # 2. 株価（日足）。流動性で絞る
    codes = sorted(c["code"].unique())
    px = yf.download([f"{k}.T" for k in codes], start="2026-09-01", end=end, progress=False,
                     auto_adjust=False, group_by="ticker", threads=True)
    keep = []
    for (d, code), g in c.groupby(["date", "code"]):
        t = f"{code}.T"
        if t not in px.columns.get_level_values(0):
            continue
        p = px[t].dropna(subset=["Open", "Close"])
        p.index = [i.date() for i in p.index]
        if d not in p.index:
            continue
        before = p[p.index < d].tail(20)
        if len(before) < 10:
            continue
        turnover = (before["Close"] * before["Volume"]).mean()
        if turnover < 5e8:
            continue
        row = p.loc[d]
        keep.append({"date": d, "code": code, "name": g["name"].iloc[0], "titles": " / ".join(g["title"]),
                     "urls": list(g["url"]), "prev_close": float(before["Close"].iloc[-1]),
                     "open": float(row["Open"]), "close": float(row["Close"]), "turnover": turnover})
    k = pd.DataFrame(keep)
    print(f"流動性で絞り込み後 {len(k)}件")
    # 3. PDF 本文の冒頭（決算短信を優先）
    from pypdf import PdfReader
    texts = []
    for r in k.itertuples():
        urls = sorted(r.urls, key=lambda u: 0)  # 題名の順（決算短信が先に来ることが多い）
        txt = ""
        for u in urls[:2]:
            p = D / "pdf" / (u.rsplit("/", 1)[-1])
            try:
                if not p.exists():
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(_get(u))
                    time.sleep(0.5)
                rd = PdfReader(io.BytesIO(p.read_bytes()))
                txt += "\n".join((pg.extract_text() or "") for pg in rd.pages[:2])[:2500] + "\n"
            except Exception as e:  # noqa: BLE001
                txt += f"（本文を読めませんでした: {type(e).__name__}）\n"
        texts.append(txt[:4000])
    k["text"] = texts
    k.to_pickle(D / "candidates.pkl")
    ok = ~k["text"].str.startswith("（本文を読めません")
    print(f"本文あり {ok.sum()}/{len(k)}件 → {D / 'candidates.pkl'}")


def prompts_for(k: pd.DataFrame) -> list[str]:
    return [f"""会社: {r.code} {r.name}
今日の日付: {r.date}（寄り付き前）
前日終値: {r.prev_close:g}

開示の題名: {r.titles}

本文の冒頭（PDFから抽出。表が崩れていることがある）:
{r.text}

今日の寄り付きで買うべきか判断してください。""" for r in k.itertuples()]


def evaluate(k: pd.DataFrame, label: str) -> str:
    k = k.copy()
    k["ret"] = k["close"] / k["open"] - 1 - COST
    k["gap"] = k["open"] / k["prev_close"] - 1
    r = k["ret"].to_numpy()
    take = k["take_long"].fillna(False).to_numpy(bool)
    p = permutation_p(r, take)
    c1 = take.any() and r[take].mean() > r.mean()
    c2 = p < 0.10
    c3 = take.any() and r[take].mean() > 0
    verdict = "効果あり" if (c1 and c2 and c3) else "見込みあり（追加検証）" if (c1 and c2) else "効果なし"
    rule = k["titles"].str.contains("上方修正|増配|自己株式").to_numpy()
    imp = k["impact"].astype(float)
    lines = [
        f"■ {label}",
        f"  全候補      {len(r):4d}件  平均 {r.mean() * 100:+.3f}%",
        f"  買う        {take.sum():4d}件  平均 {r[take].mean() * 100 if take.any() else 0:+.3f}%  合計 {r[take].sum() * 100:+.1f}%",
        f"  買わない    {(~take).sum():4d}件  平均 {r[~take].mean() * 100 if (~take).any() else 0:+.3f}%",
        f"  比較: 題名に「上方修正・増配・自己株式」 {rule.sum()}件 平均 {r[rule].mean() * 100 if rule.any() else 0:+.3f}%",
        f"  偶然でこの差が出る確率 p = {p:.3f}",
        f"  判定: {verdict}（基準1 {'○' if c1 else '×'} / 基準2 {'○' if c2 else '×'} / 基準3 {'○' if c3 else '×'}）",
        f"  読解の確認: Claudeの評価と寄り付きの窓の相関 {imp.corr(k['gap']):+.2f}"
        f"（評価+2の平均の窓 {k.loc[imp >= 2, 'gap'].mean() * 100:+.1f}% / 評価-2 {k.loc[imp <= -2, 'gap'].mean() * 100:+.1f}%）",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--budget", type=float, default=30.0)
    a = ap.parse_args()
    if a.prepare:
        prepare()
        return
    k = pd.read_pickle(D / "candidates.pkl")
    k = k[~k["text"].str.startswith("（本文を読めません")].reset_index(drop=True)
    from llm import ask_many
    res = ask_many(a.model, SYSTEM, prompts_for(k), SCHEMA, a.effort, tag="exp2", budget_usd=a.budget)
    for f in ("impact", "take_long", "confidence", "reason"):
        k[f] = [r["data"][f] if r and r["data"] else None for r in res]
    k["usd"] = [r["usage"]["usd"] if r else 0 for r in res]
    OUT.mkdir(parents=True, exist_ok=True)
    k.drop(columns=["text", "urls"]).to_csv(OUT / f"exp2_{a.model}.csv", index=False)
    ans = k["take_long"].notna()
    print(f"回答 {ans.sum()}/{len(k)}件 / 料金 ${k['usd'].sum():.2f}")
    print(evaluate(k[ans], f"{a.model}（effort={a.effort}）"))


if __name__ == "__main__":
    main()
