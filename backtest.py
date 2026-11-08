"""デイトレード戦略のバックテスト（5分足）。

ルール（不正に有利にならないよう、保守的にしている）:
- シグナルは足が確定してから判断し、次の足の始値で約定したとみなす
- 損切りと利確は足の高値・安値で判定し、同じ足で両方に届いた場合は損切りを優先
- 毎日 15:20 までに必ず決済（持ち越しなし）
- 片道ごとにスリッページと手数料を差し引く
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from data import UNIVERSE, load

SLIPPAGE = 0.0005    # 片道 0.05%（大型株の板の厚さを想定）
COMMISSION = 0.0     # 片道の手数料率。信用の手数料無料コースを想定。現物ならここを変える
FORCE_EXIT = "15:20"


@dataclass
class Trade:
    ticker: str
    date: str
    side: int            # 1=買い, -1=売り（空売り）
    entry_time: str
    entry: float
    exit_time: str
    exit: float
    reason: str
    ret: float           # コスト控除後の損益率


def _close(t: str, d, side, et, ep, xt, xp, reason) -> Trade:
    cost = 2 * (SLIPPAGE + COMMISSION)
    ret = side * (xp - ep) / ep - cost
    return Trade(t, str(d), side, et, ep, xt, xp, reason, ret)


def _run_day(t, d, day: pd.DataFrame, signal_fn, allow_short: bool, max_trades: int):
    """1日分のバーを順に見て、signal_fn が返す注文を約定させる。"""
    trades = []
    pos = None   # (side, entry_time, entry_price, stop, target)
    times = day.index.strftime("%H:%M")
    o, h, l, c = (day[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    ctx = signal_fn.prepare(day)
    pending = None
    for i in range(len(day)):
        tm = times[i]
        # 1) 前の足で出たシグナルを、この足の始値で約定
        if pending is not None and pos is None and tm < FORCE_EXIT:
            side, stop_d, tgt_d = pending
            ep = o[i]
            pos = (side, tm, ep, ep - side * stop_d * ep, ep + side * tgt_d * ep if tgt_d else None)
        pending = None
        if pos is None and tm >= FORCE_EXIT:
            break
        # 2) 保有中なら、強制決済・損切り・利確を判定
        if pos is not None:
            side, et, ep, stop, tgt = pos
            if tm >= FORCE_EXIT:
                trades.append(_close(t, d, side, et, ep, tm, o[i], "大引け前決済"))
                pos = None
                break
            hit_stop = l[i] <= stop if side == 1 else h[i] >= stop
            hit_tgt = tgt is not None and (h[i] >= tgt if side == 1 else l[i] <= tgt)
            if hit_stop:
                # 始値の時点で損切りラインを越えていたら始値で約定（窓を考慮）
                px = min(o[i], stop) if side == 1 else max(o[i], stop)
                trades.append(_close(t, d, side, et, ep, tm, px, "損切り"))
                pos = None
            elif hit_tgt:
                px = max(o[i], tgt) if side == 1 else min(o[i], tgt)
                trades.append(_close(t, d, side, et, ep, tm, px, "利確"))
                pos = None
            elif signal_fn.exit_signal(ctx, i, side):
                # 次の足の始値で決済
                if i + 1 < len(day):
                    trades.append(_close(t, d, side, et, ep, times[i + 1], o[i + 1], "手仕舞いシグナル"))
                    pos = None
            else:
                # トレーリングストップ: 確定した終値から trail 分だけ離れた位置まで損切りを引き上げる
                trail = getattr(signal_fn, "trail", None)
                if trail:
                    new = c[i] * (1 - side * trail)
                    stop = max(stop, new) if side == 1 else min(stop, new)
                    pos = (side, et, ep, stop, tgt)
            continue
        # 3) ノーポジションなら、新規のシグナルを判定（足の確定後）
        if len(trades) >= max_trades:
            continue
        sig = signal_fn.entry_signal(ctx, i, tm)
        if sig is None:
            continue
        side, stop_d, tgt_d = sig
        if side == -1 and not allow_short:
            continue
        pending = (side, stop_d, tgt_d)
    return trades


# ---------------------------------------------------------------- 戦略

class ORB:
    """寄り付きレンジ・ブレイクアウト。
    9:00〜9:30 の高値を上抜けたら買い、安値を下抜けたら売り。
    損切り: レンジの反対側（上限1.5%）、利確: 損切り幅の2倍。新規は11:00まで。
    """
    name = "寄り付きブレイクアウト"

    def __init__(self, or_end="09:30", last_entry="11:00", rr=2.0, max_stop=0.015):
        self.or_end, self.last_entry, self.rr, self.max_stop = or_end, last_entry, rr, max_stop

    def prepare(self, day):
        times = day.index.strftime("%H:%M")
        rng = day[times < self.or_end]
        return {"times": times, "close": day["Close"].to_numpy(),
                "hi": rng["High"].max() if len(rng) else np.nan,
                "lo": rng["Low"].min() if len(rng) else np.nan}

    def entry_signal(self, ctx, i, tm):
        if tm < self.or_end or tm >= self.last_entry or np.isnan(ctx["hi"]):
            return None
        c, hi, lo = ctx["close"][i], ctx["hi"], ctx["lo"]
        if c > hi:
            stop = min((c - lo) / c, self.max_stop)
            return (1, stop, stop * self.rr)
        if c < lo:
            stop = min((hi - c) / c, self.max_stop)
            return (-1, stop, stop * self.rr)
        return None

    def exit_signal(self, ctx, i, side):
        return False


class VWAPPullback:
    """VWAP押し目買い（戻り売り）。
    10:00以降、VWAPより上で推移している銘柄が VWAP まで下げて反発したら買う。
    損切り 0.6%、利確 1.2%。新規は14:00まで。
    """
    name = "VWAP押し目"

    def __init__(self, start="10:00", last_entry="14:00", stop=0.006, target=0.012):
        self.start, self.last_entry, self.stop, self.target = start, last_entry, stop, target

    def prepare(self, day):
        tp = (day["High"] + day["Low"] + day["Close"]) / 3
        vwap = (tp * day["Volume"]).cumsum() / day["Volume"].cumsum()
        return {"close": day["Close"].to_numpy(), "low": day["Low"].to_numpy(),
                "high": day["High"].to_numpy(), "open": day["Open"].to_numpy(),
                "vwap": vwap.to_numpy(), "vwap_s": vwap.shift(6).to_numpy()}

    def entry_signal(self, ctx, i, tm):
        if tm < self.start or tm >= self.last_entry or i < 6:
            return None
        v, vs = ctx["vwap"][i], ctx["vwap_s"][i]
        c, lo, hi, op = ctx["close"][i], ctx["low"][i], ctx["high"][i], ctx["open"][i]
        prev_above = np.all(ctx["close"][i - 6:i] > ctx["vwap"][i - 6:i])
        prev_below = np.all(ctx["close"][i - 6:i] < ctx["vwap"][i - 6:i])
        # 上昇中のVWAPにタッチして陽線で引けたら買い
        if v > vs and prev_above and lo <= v and c > v and c > op:
            return (1, self.stop, self.target)
        if v < vs and prev_below and hi >= v and c < v and c < op:
            return (-1, self.stop, self.target)
        return None

    def exit_signal(self, ctx, i, side):
        return False


class EMACross:
    """移動平均クロス（5分足 EMA9 / EMA26）。
    ゴールデンクロスで買い、デッドクロスで決済。損切り 0.7%。新規は14:30まで。
    """
    name = "移動平均クロス"

    def __init__(self, fast=9, slow=26, stop=0.007, last_entry="14:30"):
        self.fast, self.slow, self.stop, self.last_entry = fast, slow, stop, last_entry

    def prepare(self, day):
        # 前日までのデータが使えないので、当日の足だけで計算（序盤はシグナルを出さない）
        f = day["Close"].ewm(span=self.fast, adjust=False).mean().to_numpy()
        s = day["Close"].ewm(span=self.slow, adjust=False).mean().to_numpy()
        return {"f": f, "s": s}

    def entry_signal(self, ctx, i, tm):
        if i < self.slow // 2 or tm >= self.last_entry:
            return None
        f, s = ctx["f"], ctx["s"]
        if f[i - 1] <= s[i - 1] and f[i] > s[i]:
            return (1, self.stop, None)
        if f[i - 1] >= s[i - 1] and f[i] < s[i]:
            return (-1, self.stop, None)
        return None

    def exit_signal(self, ctx, i, side):
        f, s = ctx["f"], ctx["s"]
        return (side == 1 and f[i] < s[i]) or (side == -1 and f[i] > s[i])


class TrendHold:
    """改善版: トレンドに1回だけ乗って、利益を伸ばす。

    初回の検証で「途中の手仕舞いシグナルで降りた取引は負け、引けまで持った取引は勝ち」
    だったことから、売買の回数を減らし、利益確定の上限をなくした。
    - 9:30以降、EMA9 が EMA26 を上抜け、かつ終値が VWAP より上なら買い（売りは逆）
    - 1銘柄1日1回まで。新規は 13:00 まで
    - 損切り: 当初 0.8%、その後は終値から 1.0% 下にトレーリング（引き上げるだけ）
    - 利確の指値は置かず、トレーリングに掛かるか 15:20 まで持つ
    パラメータは事前に決めた切りの良い値で、この60日に合わせた調整はしていない。
    """
    name = "トレンド追随（改善版）"

    def __init__(self, fast=9, slow=26, stop=0.008, trail=0.010,
                 start="09:30", last_entry="13:00", use_vwap=True):
        self.fast, self.slow, self.stop, self.trail = fast, slow, stop, trail
        self.start, self.last_entry, self.use_vwap = start, last_entry, use_vwap

    def prepare(self, day):
        ctx = EMACross(self.fast, self.slow).prepare(day)
        tp = (day["High"] + day["Low"] + day["Close"]) / 3
        ctx["vwap"] = ((tp * day["Volume"]).cumsum() / day["Volume"].cumsum()).to_numpy()
        ctx["close"] = day["Close"].to_numpy()
        return ctx

    def entry_signal(self, ctx, i, tm):
        if tm < self.start or tm >= self.last_entry or i < 1:
            return None
        f, s, c, v = ctx["f"], ctx["s"], ctx["close"][i], ctx["vwap"][i]
        up = f[i - 1] <= s[i - 1] and f[i] > s[i]
        dn = f[i - 1] >= s[i - 1] and f[i] < s[i]
        if up and (not self.use_vwap or c > v):
            return (1, self.stop, None)
        if dn and (not self.use_vwap or c < v):
            return (-1, self.stop, None)
        return None

    def exit_signal(self, ctx, i, side):
        return False


class TrendHoldNoVWAP(TrendHold):
    name = "トレンド追随（VWAP条件なし）"

    def __init__(self):
        super().__init__(use_vwap=False)


STRATEGIES = {"orb": ORB, "vwap": VWAPPullback, "ema": EMACross,
              "trend": TrendHold, "trend_novwap": TrendHoldNoVWAP}


# ---------------------------------------------------------------- 集計

def run(strategy, tickers, allow_short=True, max_trades=1) -> pd.DataFrame:
    rows = []
    for t in tickers:
        df = load(t)
        for d, day in df.groupby(df.index.date):
            if len(day) < 40:   # 半日立会やデータ欠けの日は除外
                continue
            rows += [asdict(x) for x in _run_day(t, d, day, strategy, allow_short, max_trades)]
    return pd.DataFrame(rows)


def summarize(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"取引回数": 0}
    r = tr["ret"]
    wins, losses = r[r > 0], r[r <= 0]
    # 日ごとに全銘柄の損益を平均 → 資金を銘柄に均等配分した場合の日次損益
    daily = tr.groupby("date")["ret"].mean()
    eq = (1 + daily).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else np.inf
    # 前半と後半に分けて、どちらでも勝てているか（たまたまではないか）を見る
    half = len(daily) // 2
    first = (1 + daily.iloc[:half]).prod() - 1
    second = (1 + daily.iloc[half:]).prod() - 1
    return {
        "取引回数": len(r),
        "勝率": f"{(r > 0).mean():.0%}",
        "平均損益/回": f"{r.mean():+.3%}",
        "平均利益": f"{wins.mean():+.2%}" if len(wins) else "-",
        "平均損失": f"{losses.mean():+.2%}" if len(losses) else "-",
        "PF": f"{pf:.2f}",
        "期間リターン": f"{eq.iloc[-1] - 1:+.1%}",
        "最大DD": f"{dd:.1%}",
        "プラスの日": f"{(daily > 0).mean():.0%}",
        "前半": f"{first:+.1%}",
        "後半": f"{second:+.1%}",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=list(STRATEGIES) + ["all"], default="all")
    ap.add_argument("--long-only", action="store_true", help="買いのみ（現物口座を想定）")
    ap.add_argument("--slippage", type=float, default=SLIPPAGE)
    ap.add_argument("--save", action="store_true", help="取引明細を results/ に保存")
    a = ap.parse_args()
    globals()["SLIPPAGE"] = a.slippage

    names = list(STRATEGIES) if a.strategy == "all" else [a.strategy]
    tickers = list(UNIVERSE)
    out = {}
    for n in names:
        strat = STRATEGIES[n]()
        for label, short in ([("買いのみ", False)] if a.long_only else [("買いのみ", False), ("買い+売り", True)]):
            tr = run(strat, tickers, allow_short=short, max_trades=3 if n == "ema" else 1)
            out[f"{strat.name}（{label}）"] = summarize(tr)
            if a.save and not tr.empty:
                from pathlib import Path
                Path("results").mkdir(exist_ok=True)
                tr.to_csv(f"results/{n}_{'short' if short else 'long'}.csv", index=False)
    res = pd.DataFrame(out).T
    pd.set_option("display.width", 200)
    pd.set_option("display.unicode.east_asian_width", True)
    print(f"対象: {len(tickers)}銘柄 / スリッページ 片道{SLIPPAGE:.2%} / 手数料 片道{COMMISSION:.2%}\n")
    print(res.to_string())


if __name__ == "__main__":
    main()
