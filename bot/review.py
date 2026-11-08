"""ボットの成績を短く要約する（Claude に読ませるのはこの出力だけ。トークンの節約のため）。

  python3 -m bot.review                  仮想の売買の直近5日
  python3 -m bot.review --mode live --days 20
  python3 -m bot.review --mode replay    リプレイの結果
"""
from __future__ import annotations

import argparse
import json
from collections import Counter

import pandas as pd

from bot.config import ROOT


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="paper", choices=["paper", "live", "replay"])
    ap.add_argument("--days", type=int, default=5)
    a = ap.parse_args(argv)
    d = ROOT / "logs" / a.mode
    tf = d / "trades.csv"
    if not tf.exists():
        print(f"記録がありません（{d}）")
        return
    tr = pd.read_csv(tf)
    days = sorted(tr["date"].astype(str).unique())[-a.days:]
    tr = tr[tr["date"].astype(str).isin(days)]
    daily = tr.groupby("date")["pnl"].sum()
    eq = daily.cumsum()
    dd = (eq - eq.cummax()).min() if len(eq) else 0
    wins = tr[tr["pnl"] > 0]
    print(f"[{a.mode}] {days[0]}〜{days[-1]}（{len(days)}日）")
    print(f"取引 {len(tr)}回 / 勝率 {len(wins) / max(len(tr), 1):.0%} / 損益 {tr['pnl'].sum():+,}円 / "
          f"最大DD {dd:+,.0f}円 / プラスの日 {(daily > 0).mean():.0%}")
    if len(tr):
        print(f"平均 利益 {wins['pnl'].mean() if len(wins) else 0:+,.0f}円 / "
              f"損失 {tr[tr['pnl'] <= 0]['pnl'].mean() if (tr['pnl'] <= 0).any() else 0:+,.0f}円")
        print("決済理由: " + ", ".join(f"{k} {v}" for k, v in tr["reason"].value_counts().items()))
        by_t = tr.groupby("ticker")["pnl"].agg(["count", "sum"]).sort_values("sum")
        print("銘柄別（損益の小さい順）: " + ", ".join(f"{t} {int(r['count'])}回 {r['sum']:+,.0f}" for t, r in by_t.iterrows()))
    skips, errors, halts = Counter(), [], []
    for day in days:
        f = d / f"events-{day}.jsonl"
        if not f.exists():
            continue
        for line in f.open(encoding="utf-8"):
            e = json.loads(line)
            if e["kind"] == "skip":
                skips[e["reason"].split("：")[0].split(":")[0]] += 1
            elif e["kind"] == "error":
                errors.append(f"{day} {e['msg'][:80]}")
            elif e["kind"] == "halt":
                halts.append(f"{day} {e['reason']}")
    if skips:
        print("見送ったシグナル: " + ", ".join(f"{k} {v}" for k, v in skips.most_common(6)))
    print(f"停止 {len(halts)}回" + ("（" + " / ".join(halts[-3:]) + "）" if halts else ""))
    print(f"エラー {len(errors)}件" + ("（直近: " + errors[-1] + "）" if errors else ""))
    print("日別: " + " ".join(f"{str(k)[5:]} {v:+,}" for k, v in daily.items()))


if __name__ == "__main__":
    main()
