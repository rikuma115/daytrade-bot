"""ボットの起動。

  python3 -m bot.run replay --date 2026-11-06   過去の1日を再生して動作確認（発注しない）
  python3 -m bot.run replay --all               保存済みの全ての日を再生
  python3 -m bot.run live                       当日の取引時間中、5分ごとに動く（cron で平日 8:55 に起動する想定）

mode = "paper" のあいだは、live でも発注しない（仮想の売買）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

from bot import config
from bot.broker import PaperBroker, TachibanaBroker
from bot.engine import Engine
from bot.feed import JST, ReplayFeed, YahooFeed
from data import UNIVERSE, load


def replay(cfg, date: str, log_dir: Path) -> Engine:
    feed = ReplayFeed(date)
    for t in cfg.tickers:
        feed._day(t)
    eng = Engine(cfg, feed, PaperBroker(cfg.force_exit), date, log_dir, names=UNIVERSE)
    for now in feed.clock():
        eng.step(now)
    eng.step(dt.datetime.fromisoformat(f"{date}T15:35:00+09:00"))
    return eng


def live(cfg, until: str = "15:36"):
    """until（日本時間）まで5分ごとに動く。GitHub Actions では前場と後場の2回に分けて起動する。"""
    if dt.datetime.now(JST).weekday() >= 5:
        print("土日は動きません")
        return
    today = dt.datetime.now(JST).date().isoformat()
    broker = PaperBroker(cfg.force_exit) if cfg.mode == "paper" else TachibanaBroker(cfg)
    log_dir = config.ROOT / "logs" / cfg.mode
    eng = Engine(cfg, YahooFeed(), broker, today, log_dir,
                 state_file=config.ROOT / "state" / f"{today}.json", names=UNIVERSE)
    eng.journal.event("start", mode=cfg.mode, tickers=eng.tickers)
    while True:
        now = dt.datetime.now(JST)
        if now.strftime("%H:%M") >= until:
            break
        if now.strftime("%H:%M") >= "09:00":
            eng.step(now)
            if now.strftime("%H:%M") >= "09:40" and not eng.state.last_bar:
                eng.journal.event("holiday", msg="9:40 になっても株価が届かないため休場日と判断")
                print("休場日のため終了します")
                return
        # 次の5分の区切り + 20秒まで待つ（足が確定してデータが届くのを待つ）
        nxt = (now.replace(second=0, microsecond=0) + dt.timedelta(minutes=5 - now.minute % 5)
               + dt.timedelta(seconds=20))
        time.sleep(max(5, (nxt - dt.datetime.now(JST)).total_seconds()))
    if until >= "15:31" and not eng.state.summarized:
        eng.summary()
    eng.journal.event("stop", until=until)


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay")
    r.add_argument("--date")
    r.add_argument("--all", action="store_true")
    lv = sub.add_parser("live")
    lv.add_argument("--until", default="15:36", help="この時刻（日本時間 HH:MM）で終了する")
    a = ap.parse_args(argv)
    cfg = config.load()

    if a.cmd == "live":
        live(cfg, a.until)
        return
    log_dir = config.ROOT / "logs" / "replay"
    if log_dir.exists():
        for p in log_dir.glob("*"):
            p.unlink()
    dates = [a.date] if a.date else sorted({str(d) for d in load(cfg.tickers[0]).index.date})
    if not (a.date or a.all):
        sys.exit("--date か --all を指定してください")
    total = 0.0
    for d in dates:
        eng = replay(cfg, d, log_dir)
        total += eng.state.realized
        st = eng.state
        print(f"{d} 取引 {st.entries}回 / 損益 {st.realized:+8,.0f}円 / 累計 {total:+9,.0f}円"
              + (f" / 停止: {st.halt_reason}" if st.halted else ""))


if __name__ == "__main__":
    main()
