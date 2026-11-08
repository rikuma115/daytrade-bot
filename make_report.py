"""バックテスト結果を、チャートで見られるページ用のデータに書き出す。

前提: python3 backtest.py --save を実行済み（results/ に取引明細がある）
出力: report/data.json（成績と取引）、report/bars/<銘柄>.json（5分足）
"""
import json
from pathlib import Path

import pandas as pd

from data import UNIVERSE, load

ROOT = Path(__file__).parent
OUT = ROOT / "report"
STRATS = {
    "trend": "トレンド追随（改善版）",
    "ema": "移動平均クロス",
    "vwap": "VWAP押し目",
    "orb": "寄り付きブレイクアウト",
}
JST = 9 * 3600


def main():
    (OUT / "bars").mkdir(parents=True, exist_ok=True)
    variants, trades = [], []
    for key, name in STRATS.items():
        for mode, label in (("long", "買いのみ"), ("short", "買い+売り")):
            path = ROOT / "results" / f"{key}_{mode}.csv"
            tr = pd.read_csv(path)
            r = tr["ret"]
            daily = tr.groupby("date")["ret"].mean()
            eq = (1 + daily).cumprod()
            half = len(daily) // 2
            vid = f"{key}_{mode}"
            variants.append({
                "id": vid, "strategy": key, "name": name, "mode": label,
                "n": int(len(r)),
                "win": float((r > 0).mean()),
                "avg": float(r.mean()),
                "pf": float(r[r > 0].sum() / -r[r <= 0].sum()),
                "total": float(eq.iloc[-1] - 1),
                "mdd": float((eq / eq.cummax() - 1).min()),
                "first": float((1 + daily.iloc[:half]).prod() - 1),
                "second": float((1 + daily.iloc[half:]).prod() - 1),
                "equity": [[d, round(float(v - 1), 5)] for d, v in eq.items()],
            })
            for row in tr.itertuples():
                trades.append([vid, row.ticker, row.date, int(row.side), row.entry_time,
                               round(row.entry, 1), row.exit_time, round(row.exit, 1),
                               row.reason, round(row.ret, 5)])

    for t in UNIVERSE:
        df = load(t)
        secs = [int(x.timestamp()) + JST for x in df.index]   # チャートに日本時間で表示するため
        bars = [[int(s), round(o, 1), round(h, 1), round(l, 1), round(c, 1), int(v)]
                for s, o, h, l, c, v in zip(secs, df.Open, df.High, df.Low, df.Close, df.Volume)]
        (OUT / "bars" / f"{t}.json").write_text(json.dumps(bars, separators=(",", ":")))

    meta = {
        "tickers": UNIVERSE,
        "period": [str(load("7203.T").index[0].date()), str(load("7203.T").index[-1].date())],
        "slippage": 0.0005,
    }
    js = (json.dumps({"meta": meta, "variants": variants, "trades": trades},
                                          ensure_ascii=False, separators=(",", ":")))
    (OUT / "data.json").write_text(js)
    print(f"variants={len(variants)} trades={len(trades)} data.json={len(js)/1e3:.0f}KB")


if __name__ == "__main__":
    main()
