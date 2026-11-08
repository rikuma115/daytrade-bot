"""タイムマシン（日本株版）。

注文の直前に、その銘柄の過去の同じ時間帯の5分足をランダムに並べて、
その日の残りの時間を何千通りも作り、損切りに先に届く確率などを数える。

注意: これは「値動きがランダムだったら」という見積もりで、戦略が勝てるかどうかの根拠にはならない。
損切りと利確の距離、その銘柄の最近の値動きの大きさから見た、リスクの目安として使う。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from data import load


def _pool(ticker: str, before) -> dict[str, np.ndarray]:
    """時刻ごとの (始値, 高値, 安値, 終値) / 前の足の終値 の比率。当日より前の、全ての足がそろった日だけ使う。"""
    df = load(ticker)
    df = df[df.index.date < before]
    out: dict[str, list] = {}
    for _, day in df.groupby(df.index.date):
        if len(day) < 60:
            continue
        prev = day["Close"].shift(1)
        prev.iloc[0] = day["Open"].iloc[0]
        r = day[["Open", "High", "Low", "Close"]].div(prev, axis=0).to_numpy()
        for tm, row in zip(day.index.strftime("%H:%M"), r):
            out.setdefault(tm, []).append(row)
    return {k: np.array(v) for k, v in out.items()}


def simulate(ticker: str, date, entry_time: str, entry: float, side: int, stop_pct: float,
             trail: float | None = None, target_pct: float | None = None, force_exit: str = "15:20",
             replays: int = 2000, seed: int = 19, window: int = 2) -> dict | None:
    pool = _pool(ticker, date)
    slots = sorted(t for t in pool if entry_time <= t < force_exit)
    if not slots or len(pool) < 30:
        return None
    all_slots = sorted(pool)
    rng = np.random.default_rng(seed)
    n = replays
    price = np.full(n, float(entry))
    stop = entry * (1 - side * stop_pct) * np.ones(n)
    tgt = entry * (1 + side * target_pct) if target_pct else None
    done = np.zeros(n, bool)
    result = np.zeros(n)            # 損益率
    how = np.zeros(n, int)          # 1=損切り/トレーリング, 2=利確, 3=大引け前決済
    for k, s in enumerate(slots):
        i = all_slots.index(s)
        cand = np.concatenate([pool[all_slots[j]] for j in range(max(0, i - window), min(len(all_slots), i + window + 1))])
        r = cand[rng.integers(0, len(cand), n)]
        o, h, l, c = (price * r[:, j] for j in range(4))
        if k == 0:                  # 最初の足は、エントリー価格を始値とする
            o = np.full(n, float(entry))
        live = ~done
        if side == 1:
            hs = live & (l <= stop)
            px = np.minimum(o, stop)
        else:
            hs = live & (h >= stop)
            px = np.maximum(o, stop)
        result[hs] = side * (px[hs] - entry) / entry
        how[hs] = 1
        done |= hs
        if tgt is not None:
            ht = ~done & ((h >= tgt) if side == 1 else (l <= tgt))
            result[ht] = side * (tgt - entry) / entry
            how[ht] = 2
            done |= ht
        if trail:
            new = c * (1 - side * trail)
            stop = np.where(done, stop, np.maximum(stop, new) if side == 1 else np.minimum(stop, new))
        price = c
    rest = ~done
    result[rest] = side * (price[rest] - entry) / entry
    how[rest] = 3
    return {
        "stop_first": float((how == 1).mean()),
        "target_first": float((how == 2).mean()),
        "held_to_close": float((how == 3).mean()),
        "mean_ret": float(result.mean()),
        "p10": float(np.percentile(result, 10)),
        "p90": float(np.percentile(result, 90)),
        "replays": n,
    }


if __name__ == "__main__":
    import argparse
    import datetime as dt
    ap = argparse.ArgumentParser(description="タイムマシン（日本株版）")
    ap.add_argument("ticker"); ap.add_argument("--date", required=True); ap.add_argument("--time", required=True)
    ap.add_argument("--entry", type=float, required=True); ap.add_argument("--stop", type=float, default=0.008)
    ap.add_argument("--trail", type=float); ap.add_argument("--target", type=float)
    ap.add_argument("--short", action="store_true")
    a = ap.parse_args()
    r = simulate(a.ticker, dt.date.fromisoformat(a.date), a.time, a.entry, -1 if a.short else 1,
                 a.stop, a.trail, a.target)
    print(r if r else "過去データが足りません")
