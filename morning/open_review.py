"""寄り付き後の見直しの資料づくり。朝の買い候補が、実際にどう始まったかをまとめる。

  python -m morning.open_review              今日の資料を morning/briefs/<日付>.open.md に作る
  python -m morning.open_review --date 2026-11-06 --at 09:50   過去の日で試す

注意: Yahoo Finance の日本株の5分足は遅れて届く（目安20分）。資料には「何時の足まで見えているか」を書く。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from morning.gather import BRIEFS, JST, is_trading_day

ROOT = Path(__file__).resolve().parent.parent
PICKS = ROOT / "picks"


def bars_today(codes: list[str], d: dt.date, at: dt.datetime) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    out = {}
    raw = yf.download([f"{c}.T" for c in codes], period="5d", interval="5m", progress=False,
                      auto_adjust=False, group_by="ticker")
    for c in codes:
        t = f"{c}.T"
        if t not in raw.columns.get_level_values(0):
            continue
        df = raw[t].dropna(subset=["Close"])
        df.index = pd.to_datetime(df.index, utc=True).tz_convert("Asia/Tokyo")
        df = df[(df.index.date == d) & (df.index + pd.Timedelta(minutes=5) <= at)]
        if len(df):
            out[c] = df
    return out


def build(d: dt.date, at: dt.datetime) -> Path | None:
    if not is_trading_day(d):
        print(f"{d} は休場日です。")
        return None
    pf = PICKS / f"{d}.json"
    if not pf.exists():
        print("今朝の注目銘柄がありません。見直しは不要です。")
        return None
    picks = [p for p in json.loads(pf.read_text(encoding="utf-8"))["picks"] if p["action"] == "long"]
    if not picks:
        print("今朝の買い候補が0社なので、見直しは不要です。")
        return None
    cands = {c["code"]: c for c in json.loads((BRIEFS / f"{d}.candidates.json").read_text(encoding="utf-8"))}
    bars = bars_today([p["code"] for p in picks], d, at)
    lines = [f"# {d} 寄り付き後の見直し（作成 {at:%H:%M}）", "",
             "5分足は遅れて届くため、各銘柄の「見えている最後の足」の時刻を確認すること。", ""]
    for p in picks:
        c = p["code"]
        prev = cands.get(c, {}).get("prev_close")
        lines.append(f"## {c} {p['name']}（朝の判断: 確信度{p['confidence']}、{p['reason']}）")
        df = bars.get(c)
        if df is None or not prev:
            lines += ["- 今日の5分足をまだ取得できない", ""]
            continue
        o, last = float(df["Open"].iloc[0]), float(df["Close"].iloc[-1])
        tp = (df["High"] + df["Low"] + df["Close"]) / 3
        vwap = float((tp * df["Volume"]).sum() / df["Volume"].sum())
        turnover = float((df["Close"] * df["Volume"]).sum())
        lines += [
            f"- 見えている最後の足: {df.index[-1]:%H:%M}（{len(df)}本）",
            f"- 前日終値 {prev:,.1f} → 始値 {o:,.1f}（窓 {(o / prev - 1) * 100:+.1f}%）→ 現在 {last:,.1f}（始値から {(last / o - 1) * 100:+.1f}%）",
            f"- 高値 {df['High'].max():,.1f} / 安値 {df['Low'].min():,.1f} / VWAP {vwap:,.1f}（現在値は VWAP の{'上' if last > vwap else '下'}）",
            f"- ここまでの売買代金 {turnover / 1e8:,.1f}億円（過去20日の1日平均 {cands[c]['turnover'] / 1e8:,.0f}億円）",
            "- 5分足（時刻,始値,高値,安値,終値）: " + " | ".join(
                f"{i:%H:%M},{r.Open:g},{r.High:g},{r.Low:g},{r.Close:g}" for i, r in df.iterrows()),
            "",
        ]
    path = BRIEFS / f"{d}.open.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"資料: {path}（{len(picks)}社）")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--at", help="HH:MM（過去の日で試すとき）")
    a = ap.parse_args()
    now = dt.datetime.now(JST)
    d = dt.date.fromisoformat(a.date) if a.date else now.date()
    at = dt.datetime.combine(d, dt.time.fromisoformat(a.at), JST) if a.at else now
    build(d, at)


if __name__ == "__main__":
    main()
