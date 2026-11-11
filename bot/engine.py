"""売買の本体。戦略の判断は backtest.py の戦略クラスをそのまま使う（検証と本番で同じコード）。

1本の5分足が確定するたびに、銘柄ごとに次の順で処理する（backtest.py と同じ順序）:
  1. 証券会社側の約定を反映（前の足で出した注文、損切りの逆指値）
  2. 保有中なら: 15:20 の強制決済 → 手仕舞いシグナル → トレーリングで損切りを引き上げ
  3. 保有していなければ: 戦略のシグナル → リスクチェック → タイムマシン → 発注
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

import backtest
from bot import notify, risk, timemachine
from bot.config import ROOT

BAR = dt.timedelta(minutes=5)


@dataclass
class DayState:
    date: str
    entries: int = 0
    realized: float = 0.0
    traded_today: list = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""
    last_bar: dict = field(default_factory=dict)
    open_entries: dict = field(default_factory=dict)   # ticker -> 約定した新規注文の情報
    errors: int = 0
    summarized: bool = False


class Journal:
    """全ての判断と約定を記録する。あとで振り返るための唯一の情報源。"""

    def __init__(self, log_dir: Path, date: str, notify: bool):
        self.dir = log_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events = self.dir / f"events-{date}.jsonl"
        self.trades = self.dir / "trades.csv"
        self.notify_enabled = notify

    def event(self, kind: str, **kw):
        rec = {"kind": kind, **kw}
        with self.events.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def trade(self, row: dict):
        new = not self.trades.exists()
        with self.trades.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)

    def notify(self, text: str, urgent: bool = False):
        """スマホに通知（bot/notify.py）。失敗しても売買は止めない。"""
        self.event("notify", text=text)
        if not self.notify_enabled:
            return
        for err in notify.send(text, urgent=urgent):
            self.event("notify_error", error=err)


class Engine:
    def __init__(self, cfg, feed, broker, date: str, log_dir: Path, state_file: Path | None = None,
                 names: dict | None = None):
        self.cfg, self.feed, self.broker = cfg, feed, broker
        if cfg.strategy == "picks":
            from bot.picks_strategy import PicksStrategy
            self.strategy = PicksStrategy(date, cfg.max_loss_per_trade, use_git=state_file is not None)
        else:
            self.strategy = backtest.STRATEGIES[cfg.strategy](**cfg.strategy_params)
        self.trail = getattr(self.strategy, "trail", None)
        self.state_file = state_file
        self.names = names or {}
        self.journal = Journal(log_dir, date, notify=cfg.notify_on_trade)
        self.state = DayState(date)
        self.catchup_cutoff = None   # これより前の足では新規の売買をしない（遅れて起動したとき用）
        self._load()
        # Claude の銘柄で売買するときは、見直しが届いてから銘柄が決まる
        self.tickers = [] if cfg.strategy == "picks" else self._affordable(cfg.tickers)

    # ---------- 状態の保存（途中で落ちても、再起動で続きから動けるように）
    def _load(self):
        if self.state_file and self.state_file.exists():
            d = json.loads(self.state_file.read_text())
            if d["state"]["date"] == self.state.date:
                self.state = DayState(**d["state"])
                if hasattr(self.broker, "restore"):
                    self.broker.restore(d.get("broker", {}))
                self.journal.event("restored", state=d["state"])

    def _save(self):
        if not self.state_file:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        d = {"state": asdict(self.state)}
        if hasattr(self.broker, "dump"):
            d["broker"] = self.broker.dump()
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, default=str))
        tmp.replace(self.state_file)

    def _affordable(self, tickers):
        """売買できる銘柄だけ残す（直近60日の保存データで判定）:
        売買代金が十分 / 資金で100株買える / 損切りに掛かっても1回の損失の上限に収まる。"""
        from data import load
        stop = getattr(self.strategy, "stop", None) or 0.01
        out, skipped = [], {}
        for t in tickers:
            try:
                d = load(t)
                last = float(d["Close"].iloc[-1])
                turnover = float((d["Close"] * d["Volume"]).groupby(d.index.date).sum().tail(20).mean())
            except Exception:  # noqa: BLE001
                skipped[t] = "データなし"
                continue
            if turnover < self.cfg.min_turnover:
                skipped[t] = "売買代金が少ない"
            elif last * risk.LOT > self.cfg.capital:
                skipped[t] = "100株を買う資金が足りない"
            elif last * stop * risk.LOT > self.cfg.max_loss_per_trade:
                skipped[t] = "損切りに掛かると1回の損失の上限を超える"
            else:
                out.append(t)
        if skipped:
            self.journal.event("universe", trade=len(out), skipped=skipped)
        return out

    def _label(self, t):
        return f"{t.replace('.T', '')} {self.names.get(t, '')}".strip()

    # ---------- 1ステップ（時刻 now までに確定した足を全て処理）
    def step(self, now: dt.datetime):
        errors_before = self.state.errors
        if hasattr(self.strategy, "refresh"):
            new = [t for t in self.strategy.refresh() if t not in self.tickers]
            if new:
                self.tickers += new
                self.journal.event("picks", tickers=new, detail=self.strategy.keep)
                self.journal.notify("👀 Claudeが残した銘柄の監視を始めます: "
                                    + "、".join(self._label(t) for t in new))
        for t in self.tickers:
            try:
                bars = self.feed.bars(t, now)
            except Exception as e:  # noqa: BLE001
                self._error(f"{t} のデータ取得に失敗: {e}")
                continue
            last = self.state.last_bar.get(t)
            for i in range(len(bars)):
                ts = bars.index[i]
                if last and ts.isoformat() <= last:
                    continue
                if self.catchup_cutoff is not None and ts < self.catchup_cutoff:
                    self.state.last_bar[t] = ts.isoformat()   # 見ただけにして、さかのぼって売買しない
                    continue
                try:
                    self._on_bar(t, bars.iloc[: i + 1], i)
                except Exception as e:  # noqa: BLE001
                    self._error(f"{t} の処理でエラー: {e}")
                self.state.last_bar[t] = ts.isoformat()
        # 足の到着とは別に、時計でも決済時刻を確認する（データが欠けても決済が漏れないように）
        if now.strftime("%H:%M") >= self.cfg.force_exit:
            for t in self.broker.positions():
                self.broker.exit(t, "大引け前決済")
        if now.strftime("%H:%M") >= "15:31":
            self._end_of_day(now)
        if self.state.errors == errors_before:   # このステップでエラーがなければ、連続エラーの数を戻す
            self.state.errors = 0
        if not self.state.summarized and now.strftime("%H:%M") >= "15:31":
            self.summary()
        self._save()

    def _end_of_day(self, now):
        """引け後に建玉が残っていたら異常。仮想なら最後の値で決済、本番なら至急の通知。"""
        left = self.broker.positions()
        if not left:
            return
        if hasattr(self.broker, "close_now"):
            for t in left:
                bars = self.feed.bars(t, now)
                f = self.broker.close_now(t, float(bars["Close"].iloc[-1]), bars.index[-1].strftime("%H:%M"),
                                          "引け後の強制決済（データ欠け）")
                self._on_fill(f, now.date())
            self.journal.event("error", msg=f"引け後に建玉が残っていた: {list(left)}")
        else:
            self.journal.notify(f"🚨 引け後も建玉が残っています: {list(left)}。証券会社のサイトで確認してください", urgent=True)

    def _error(self, msg):
        self.state.errors += 1
        self.journal.event("error", msg=msg)
        if self.state.errors >= 3 and not self.state.halted:
            self.halt("エラーが続いたため")

    def halt(self, reason: str):
        self.state.halted, self.state.halt_reason = True, reason
        for t in list(self.broker.positions()):
            self.broker.exit(t, f"停止: {reason}")
        self.journal.event("halt", reason=reason)
        self.journal.notify(f"⚠️ ボットを停止し、保有中の銘柄を決済します（{reason}）", urgent=True)

    def _on_bar(self, t, day, i):
        bar = day.iloc[i]
        tm = bar.name.strftime("%H:%M")
        held_at_start = t in self.broker.positions() or t in self.broker.pending()

        # 1. 約定の反映
        for f in self.broker.sync(t, bar):
            self._on_fill(f, bar.name.date())

        # 2. 保有中の管理
        pos = self.broker.positions().get(t)
        if pos:
            next_open = (bar.name + BAR).strftime("%H:%M")
            ctx = self.strategy.prepare(day)
            if next_open >= self.cfg.force_exit:
                self.broker.exit(t, "大引け前決済")
            elif self.strategy.exit_signal(ctx, i, pos.side):
                self.broker.exit(t, "手仕舞いシグナル")
            elif self.trail:
                new = float(bar["Close"]) * (1 - pos.side * self.trail)
                self.broker.move_stop(t, new)
            return
        if held_at_start:
            return

        # 3. 新規のシグナル
        ctx = self.strategy.prepare(day)
        if hasattr(self.strategy, "refresh"):
            sig = self.strategy.entry_signal(ctx, i, tm, ticker=t)
        else:
            sig = self.strategy.entry_signal(ctx, i, tm)
        if sig is None:
            return
        side, stop_pct, tgt_pct = sig
        price = float(bar["Close"])
        d = risk.check(self.cfg, ticker=t, side=side, price=price, stop_pct=stop_pct, tm=tm,
                       state=_StateView(self.state), open_positions=self.broker.positions(),
                       pending=self.broker.pending())
        rec = {"ticker": t, "time": tm, "side": side, "price": price, "stop_pct": round(stop_pct, 5)}
        if not d.ok:
            self.journal.event("skip", **rec, reason=d.reason)
            return
        odds = None
        if self.cfg.tm_enabled:
            entry_slot = (bar.name + BAR).strftime("%H:%M")
            odds = timemachine.simulate(t, bar.name.date(), entry_slot, price, side, stop_pct,
                                        self.trail, tgt_pct, self.cfg.force_exit, self.cfg.tm_replays)
            if odds and odds["stop_first"] > self.cfg.tm_block_above:
                self.journal.event("skip", **rec, reason=f"タイムマシン: 損切りが先 {odds['stop_first']:.0%}", odds=odds)
                return
        self.broker.enter(t, side, d.qty, stop_pct, tgt_pct)
        self.state.entries += 1
        self.state.traded_today.append(t)
        self.journal.event("order", **rec, qty=d.qty, odds=odds)

    def _on_fill(self, f, date):
        if f.kind == "entry":
            self.state.open_entries[f.ticker] = {"price": f.price, "time": f.time, "qty": f.qty, "side": f.side}
            self.journal.event("fill", fill=asdict(f))
            if self.cfg.notify_on_trade:
                self.journal.notify(f"{'買い' if f.side == 1 else '空売り'} {self._label(f.ticker)} "
                                    f"{f.qty}株 @{f.price:,.1f}（{f.time}）")
            return
        e = self.state.open_entries.pop(f.ticker, None)
        if e is None:
            self.journal.event("error", msg=f"{f.ticker}: 新規の記録がない決済", fill=asdict(f))
            return
        gross = f.side * (f.price - e["price"]) * f.qty
        cost = self.cfg.slippage * (e["price"] + f.price) * f.qty
        pnl = gross - cost
        self.state.realized += pnl
        self.journal.event("fill", fill=asdict(f), pnl=round(pnl))
        self.journal.trade({
            "date": str(date), "ticker": f.ticker, "side": f.side, "qty": f.qty,
            "entry_time": e["time"], "entry": e["price"], "exit_time": f.time, "exit": f.price,
            "reason": f.reason, "pnl": round(pnl), "ret": round(pnl / (e["price"] * f.qty), 5),
            "mode": self.cfg.mode, "strategy": self.cfg.strategy,
        })
        if self.cfg.notify_on_trade:
            self.journal.notify(f"決済 {self._label(f.ticker)} {f.reason} {pnl:+,.0f}円"
                                f"（本日計 {self.state.realized:+,.0f}円）")
        if self.state.realized <= -self.cfg.daily_loss_limit and not self.state.halted:
            self.halt(f"1日の損失が上限 {self.cfg.daily_loss_limit:,}円 に達した")

    def summary(self):
        s = self.state
        self.state.summarized = True
        text = (f"📒 {s.date} {'仮想' if self.cfg.mode == 'paper' else '本番'} "
                f"取引 {s.entries}回 / 損益 {s.realized:+,.0f}円"
                + (f" / 停止: {s.halt_reason}" if s.halted else ""))
        self.journal.event("summary", entries=s.entries, realized=round(s.realized), halted=s.halted)
        if self.cfg.notify_on_summary:
            self.journal.notify(text)
        return text


class _StateView:
    """risk.check に渡す読み取り専用の状態。"""

    def __init__(self, s: DayState):
        self.halted, self.halt_reason = s.halted, s.halt_reason
        self.entries, self.realized = s.entries, s.realized
        self.traded_today = set(s.traded_today)
