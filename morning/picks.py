"""Claude の朝の判断（picks/<日付>.json）の確認・通知・成績の集計。

  python -m morning.picks validate picks/2026-11-09.json   形式の確認（Claude が保存前に実行）
  python -m morning.picks notify picks/2026-11-09.json     スマホに通知（GitHub Actions が実行）
  python -m morning.picks evaluate                         引け後に、選んだ銘柄の実際の値動きを集計

picks/<日付>.json の形:
{
  "date": "2026-11-09",
  "market_view": "地合いの見立て（80字以内）",
  "picks": [
    {"code": "6976", "name": "太陽誘電", "action": "long", "confidence": 70, "reason": "80字以内"}
  ]
}
action: long = 寄り付きで買う候補 / avoid = 買わない方がよい（悪材料、出尽くし など）
最初の数週間は記録だけ。ボットの売買には使わない。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PICKS = ROOT / "picks"
BRIEFS = ROOT / "morning" / "briefs"
RESULTS = PICKS / "results.csv"
COST = 0.001


def validate(path: str) -> list[str]:
    errs = []
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [f"JSON として読めない: {e}"]
    for k in ("date", "market_view", "picks"):
        if k not in d:
            errs.append(f"{k} がない")
    if Path(path).stem != d.get("date"):
        errs.append("ファイル名と date が一致しない")
    if len(d.get("market_view", "")) > 120:
        errs.append("market_view が長すぎる（120字以内）")
    cand = BRIEFS / f"{d.get('date')}.candidates.json"
    codes = {c["code"] for c in json.loads(cand.read_text())} if cand.exists() else None
    for i, p in enumerate(d.get("picks", [])):
        for k in ("code", "name", "action", "confidence", "reason"):
            if k not in p:
                errs.append(f"picks[{i}] に {k} がない")
        if p.get("action") not in ("long", "avoid"):
            errs.append(f"picks[{i}].action は long か avoid")
        if not (0 <= int(p.get("confidence", -1)) <= 100):
            errs.append(f"picks[{i}].confidence は 0〜100")
        if codes is not None and p.get("code") not in codes:
            errs.append(f"picks[{i}].code {p.get('code')} は今朝の資料の候補にない")
    return errs


def notify(path: str):
    from bot import notify as n
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    longs = [p for p in d["picks"] if p["action"] == "long"]
    avoids = [p for p in d["picks"] if p["action"] == "avoid"]
    lines = [f"🌅 {d['date']} Claudeの注目銘柄（記録のみ・売買はしません）", f"地合い: {d['market_view']}"]
    if longs:
        lines.append("▲ 買い候補")
        lines += [f"・{p['code']} {p['name']}（確信度{p['confidence']}）{p['reason']}" for p in longs]
    else:
        lines.append("▲ 買い候補: なし")
    if avoids:
        lines.append("▼ 避ける: " + "、".join(f"{p['code']} {p['name']}" for p in avoids))
    errs = n.send("\n".join(lines), title="朝の注目銘柄")
    print("送信しました" if not errs else errs)


def evaluate() -> str:
    """まだ集計していない日の、候補全銘柄の 始値→終値 を記録し、累計の比較を返す。"""
    import yfinance as yf
    done = set(pd.read_csv(RESULTS, dtype={"code": str})["date"].astype(str)) if RESULTS.exists() else set()
    new_rows = []
    for f in sorted(PICKS.glob("20*.json")):
        date = f.stem
        cand = BRIEFS / f"{date}.candidates.json"
        if date in done or not cand.exists():
            continue
        picks = {p["code"]: p for p in json.loads(f.read_text(encoding="utf-8"))["picks"]}
        cands = json.loads(cand.read_text(encoding="utf-8"))
        if not cands:
            continue
        px = yf.download([f"{c['code']}.T" for c in cands], start=date,
                         end=str(pd.Timestamp(date) + pd.Timedelta(days=1))[:10],
                         interval="1d", progress=False, auto_adjust=False, group_by="ticker")
        if px.empty:
            continue  # まだ当日の値がない
        for c in cands:
            t = f"{c['code']}.T"
            if t not in px.columns.get_level_values(0):
                continue
            row = px[t].dropna(subset=["Open", "Close"])
            if row.empty:
                continue
            o, cl = float(row["Open"].iloc[0]), float(row["Close"].iloc[0])
            p = picks.get(c["code"], {})
            new_rows.append({"date": date, "code": c["code"], "name": c["name"],
                             "action": p.get("action", "none"), "confidence": p.get("confidence"),
                             "gap": round(o / c["prev_close"] - 1, 5), "ret": round(cl / o - 1 - COST, 5)})
    if new_rows:
        df = pd.DataFrame(new_rows)
        df.to_csv(RESULTS, mode="a", header=not RESULTS.exists(), index=False)
    return summary()


def summary() -> str:
    if not RESULTS.exists():
        return "Claudeの注目銘柄: まだ集計できる日がありません"
    df = pd.read_csv(RESULTS, dtype={"code": str})
    days = df["date"].nunique()
    def line(label, m):
        s = df[m]["ret"]
        return f"{label} {len(s)}件 平均 {s.mean() * 100:+.2f}%" if len(s) else f"{label} 0件"
    last = df["date"].max()
    today = df[(df["date"] == last) & (df["action"] == "long")]
    t = "、".join(f"{r.code}{r.name} {r.ret * 100:+.1f}%" for r in today.itertuples()) or "なし"
    return (f"🧪 Claudeの注目銘柄（記録{days}日・始値買い→終値売り・コスト込み）\n"
            f"{line('買い候補', df['action'] == 'long')} / {line('候補全体', df['action'].notna())} / "
            f"{line('避ける', df['action'] == 'avoid')}\n今日の買い候補: {t}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "validate":
        errs = validate(sys.argv[2])
        print("OK" if not errs else "\n".join(errs))
        sys.exit(1 if errs else 0)
    elif cmd == "notify":
        notify(sys.argv[2])
    elif cmd == "evaluate":
        print(evaluate())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
