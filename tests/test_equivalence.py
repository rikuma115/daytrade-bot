"""ボット（bot/engine.py）がバックテスト（backtest.py）と同じ売買をするかの照合。

リスクの上限をなくし、全銘柄・全日を再生して、取引の時刻と価格が一致するかを確かめる。
実行: python3 tests/test_equivalence.py
"""
import dataclasses
import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backtest  # noqa: E402
from bot import config  # noqa: E402
from bot.broker import PaperBroker  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.feed import ReplayFeed  # noqa: E402
from data import UNIVERSE, load  # noqa: E402
import datetime as dt  # noqa: E402


def main(strategy="trend"):
    cfg = dataclasses.replace(
        config.load(), strategy=strategy, strategy_params={}, account="cash",
        capital=10**15, max_loss_per_trade=10**6, daily_loss_limit=10**12,
        max_trades_per_day=10**6, max_positions=10**6, tm_enabled=False,
        entry_start="00:00", entry_end="23:59", tickers=list(UNIVERSE),
        notify_on_trade=False, notify_on_summary=False)
    tmp = Path(tempfile.mkdtemp())
    dates = sorted({str(d) for d in load("7203.T").index.date})
    for d in dates:
        feed = ReplayFeed(d)
        for t in cfg.tickers:
            feed._day(t)
        eng = Engine(cfg, feed, PaperBroker(cfg.force_exit), d, tmp)
        for now in feed.clock():
            eng.step(now)
    bot = pd.read_csv(tmp / "trades.csv")
    bt = backtest.run(backtest.STRATEGIES[strategy](), list(UNIVERSE), allow_short=False, max_trades=1)
    key = ["ticker", "date", "entry_time", "exit_time"]
    bot["date"] = bot["date"].astype(str)
    m = bt.merge(bot, on=key, how="outer", suffixes=("_bt", "_bot"), indicator=True)
    both = m[m["_merge"] == "both"]
    price_ok = ((both["entry_bt"] - both["entry_bot"]).abs() < 1e-6) & ((both["exit_bt"] - both["exit_bot"]).abs() < 1e-6)
    print(f"バックテスト {len(bt)}件 / ボット {len(bot)}件 / 時刻が一致 {len(both)}件 / 価格も一致 {int(price_ok.sum())}件")
    only_bt, only_bot = m[m["_merge"] == "left_only"], m[m["_merge"] == "right_only"]
    if len(only_bt) or len(only_bot) or not price_ok.all():
        print("不一致の例:")
        print(m[(m["_merge"] != "both")].head(10)[key + ["_merge"]].to_string())
        print(both[~price_ok].head(5)[key + ["entry_bt", "entry_bot", "exit_bt", "exit_bot"]].to_string())
        sys.exit(1)
    print("OK: ボットはバックテストと同じ売買をしています")


if __name__ == "__main__":
    main(*sys.argv[1:])
