"""実験1: ボットのシグナルに「乗るか見送るか」を Claude に判断させると、成績は良くなるか。

方法:
  - 戦略「トレンド追随（買いのみ）」の60日分のシグナル（約550件）を対象にする
  - 各シグナルについて、その時点までに見えている情報だけを Claude に渡す
    （当日の5分足、過去5日の日足、市場全体の動き、戦略のルール）
  - Claude が「乗る」と答えたシグナルだけで売買した場合と、全部に乗った場合を比べる
  - 株価データは 2026年8〜11月。Claude の知識は 2026年6月までなので、結果を知らない状態で判断している

事前に決めた合格基準（結果を見てから変えない）:
  1. 「乗る」と答えたシグナルの平均損益が、全シグナルの平均を上回る
  2. ランダムに同じ件数を選んだ場合と比べて、偶然では起きにくい（並べ替え検定で p < 0.10）
  3. 「乗る」だけの成績が、コスト控除後で前半・後半ともプラス
  1〜3を全て満たしたら「効果あり」、1と2だけなら「見込みあり（追加検証）」、それ以外は「効果なし」

  python3 experiments/exp1_signals.py --prepare           プロンプトを作って件数と料金の見積もりだけ出す
  python3 experiments/exp1_signals.py --model claude-haiku-5-5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))
import backtest  # noqa: E402
from data import UNIVERSE, load  # noqa: E402

OUT = ROOT / "experiments" / "results"

SYSTEM = """あなたは日本株のデイトレードの判断を補助する。
自動売買ボットが買いシグナルを出した。そのシグナルに乗るか見送るかを判断する。

ボットのルール（変えられない）:
- 5分足の EMA9 が EMA26 を上抜け、かつ終値が当日の VWAP より上で買いシグナル
- 次の5分足の始値で買う。損切りは買値の 0.8% 下。その後は5分足の終値から 1.0% 下まで損切りを引き上げる
- 利益確定の指値はなく、損切りに掛からなければ 15:20 に決済する
- 売買のたびに往復で約 0.1% のコストがかかる

渡される情報は、シグナルが出た時点までに見えているものだけ。
平均すると、このシグナルは損益がほぼゼロで、コストの分だけ負けている。
「この後 15:20 までに、コストを上回る値上がりが見込めるか」で判断する。
迷う場合は見送る。理由は60字以内で書く。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "take": {"type": "boolean"},
        "confidence": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["take", "confidence", "reason"],
    "additionalProperties": False,
}


def _daily(df):
    g = df.groupby(df.index.date)
    return pd.DataFrame({"open": g["Open"].first(), "high": g["High"].max(), "low": g["Low"].min(),
                         "close": g["Close"].last(), "volume": g["Volume"].sum()})


def build():
    """シグナルと、その時点の情報を書いたプロンプトを作る（作成に数分かかるので保存して使い回す）。"""
    cache = OUT / "exp1_prompts.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    rows, prompts = _build()
    OUT.mkdir(parents=True, exist_ok=True)
    pd.to_pickle((rows, prompts), cache)
    return rows, prompts


def _build():
    trades = backtest.run(backtest.STRATEGIES["trend"](), list(UNIVERSE), allow_short=False, max_trades=1)
    data = {t: load(t) for t in UNIVERSE}
    daily = {t: _daily(df) for t, df in data.items()}
    rows, prompts = [], []
    for tr in trades.itertuples():
        df = data[tr.ticker]
        d = pd.Timestamp(tr.date).date()
        day = df[df.index.date == d]
        sig_time = (pd.Timestamp(f"{tr.date} {tr.entry_time}") - pd.Timedelta(minutes=5)).strftime("%H:%M")
        seen = day[day.index.strftime("%H:%M") <= sig_time]
        if seen.empty:
            continue
        dd = daily[tr.ticker]
        hist = dd[dd.index < d].tail(5)
        prev_close = hist["close"].iloc[-1] if len(hist) else np.nan
        tp = (seen["High"] + seen["Low"] + seen["Close"]) / 3
        vwap = (tp * seen["Volume"]).sum() / seen["Volume"].sum()
        c = seen["Close"]
        ema9, ema26 = c.ewm(span=9, adjust=False).mean().iloc[-1], c.ewm(span=26, adjust=False).mean().iloc[-1]
        # 市場全体: 20銘柄の、寄り付きからシグナル時点までの平均騰落率
        mk = []
        for t2, df2 in data.items():
            s2 = df2[(df2.index.date == d) & (df2.index.strftime("%H:%M") <= sig_time)]
            if len(s2):
                mk.append(s2["Close"].iloc[-1] / s2["Open"].iloc[0] - 1)
        avg_vol = hist["volume"].mean() if len(hist) else np.nan
        bars = "\n".join(f"{i.strftime('%H:%M')},{r.Open:g},{r.High:g},{r.Low:g},{r.Close:g},{int(r.Volume)}"
                         for i, r in seen.iterrows())
        hist_txt = "\n".join(f"{i},{r.open:g},{r.high:g},{r.low:g},{r.close:g},{int(r.volume)}"
                             for i, r in hist.iterrows()) or "（データなし）"
        prompt = f"""銘柄: {tr.ticker.replace('.T', '')} {UNIVERSE[tr.ticker]}
日付: {tr.date}　シグナルの時刻: {sig_time}（この足の確定時点）
前日終値: {prev_close:g}　当日始値: {seen['Open'].iloc[0]:g}　現在値: {c.iloc[-1]:g}
VWAP: {vwap:.1f}　EMA9: {ema9:.1f}　EMA26: {ema26:.1f}
当日の出来高（ここまで）: {int(seen['Volume'].sum())}　過去5日の1日平均出来高: {avg_vol:.0f}
市場全体（主要20銘柄の平均、寄り付きからここまで）: {np.mean(mk) * 100:+.2f}%

過去5日の日足（日付,始値,高値,安値,終値,出来高）:
{hist_txt}

当日の5分足（時刻,始値,高値,安値,終値,出来高）:
{bars}

このシグナルに乗るか判断してください。"""
        rows.append({"ticker": tr.ticker, "date": tr.date, "entry_time": tr.entry_time, "ret": tr.ret})
        prompts.append(prompt)
    return pd.DataFrame(rows), prompts


def permutation_p(rets: np.ndarray, mask: np.ndarray, n: int = 20000, seed: int = 0) -> float:
    """同じ件数をランダムに選んだとき、平均が今回以上になる確率。"""
    k = int(mask.sum())
    if k == 0 or k == len(rets):
        return float("nan")
    obs = rets[mask].mean()
    rng = np.random.default_rng(seed)
    sims = np.array([rets[rng.choice(len(rets), k, replace=False)].mean() for _ in range(n)])
    return float((sims >= obs).mean())


def evaluate(df: pd.DataFrame, label: str) -> str:
    r = df["ret"].to_numpy()
    take = df["take"].fillna(False).to_numpy(bool)
    dates = sorted(df["date"].unique())
    half = dates[len(dates) // 2]
    t = df[take]
    first = t[t["date"] < half]["ret"].sum()
    second = t[t["date"] >= half]["ret"].sum()
    p = permutation_p(r, take)
    c1 = take.any() and r[take].mean() > r.mean()
    c2 = p < 0.10
    c3 = first > 0 and second > 0
    verdict = "効果あり" if (c1 and c2 and c3) else "見込みあり（追加検証）" if (c1 and c2) else "効果なし"
    lines = [
        f"■ {label}",
        f"  全シグナル  {len(r):4d}件  平均 {r.mean() * 100:+.3f}%  合計 {r.sum() * 100:+.1f}%",
        f"  乗る        {take.sum():4d}件  平均 {r[take].mean() * 100 if take.any() else 0:+.3f}%  合計 {r[take].sum() * 100:+.1f}%"
        f"  （前半 {first * 100:+.1f}% / 後半 {second * 100:+.1f}%）",
        f"  見送る      {(~take).sum():4d}件  平均 {r[~take].mean() * 100 if (~take).any() else 0:+.3f}%",
        f"  偶然でこの差が出る確率 p = {p:.3f}",
        f"  判定: {verdict}（基準1 {'○' if c1 else '×'} / 基準2 {'○' if c2 else '×'} / 基準3 {'○' if c3 else '×'}）",
    ]
    for th in (60, 70, 80):
        m = take & (df["confidence"].fillna(0).to_numpy() >= th)
        if m.any():
            lines.append(f"  参考: 確信度{th}以上 {m.sum():3d}件 平均 {r[m].mean() * 100:+.3f}%")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--budget", type=float, default=30.0, help="この実行で使ってよい上限（ドル）")
    a = ap.parse_args()
    rows, prompts = build()
    chars = sum(len(p) for p in prompts) + len(SYSTEM) * len(prompts)
    print(f"シグナル {len(prompts)}件 / プロンプト合計 約{chars / 1e3:.0f}千文字")
    if a.prepare:
        Path(OUT).mkdir(parents=True, exist_ok=True)
        (OUT / "exp1_sample_prompt.txt").write_text(SYSTEM + "\n\n---\n\n" + prompts[0])
        print(f"例: {OUT / 'exp1_sample_prompt.txt'}")
        return
    from llm import ask_many
    res = ask_many(a.model, SYSTEM, prompts, SCHEMA, a.effort, tag="exp1", budget_usd=a.budget)
    rows["take"] = [r["data"]["take"] if r and r["data"] else None for r in res]
    rows["confidence"] = [r["data"]["confidence"] if r and r["data"] else None for r in res]
    rows["reason"] = [r["data"]["reason"] if r and r["data"] else None for r in res]
    rows["usd"] = [r["usage"]["usd"] if r else 0 for r in res]
    OUT.mkdir(parents=True, exist_ok=True)
    rows.to_csv(OUT / f"exp1_{a.model}.csv", index=False)
    answered = rows["take"].notna()
    print(f"回答 {answered.sum()}/{len(rows)}件 / 料金 ${rows['usd'].sum():.2f}")
    print(evaluate(rows[answered].copy(), f"{a.model}（effort={a.effort}）"))


if __name__ == "__main__":
    main()
