"""株価データの供給元。どれも「今の時刻までに確定した、当日の5分足」を返す。"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from data import load

JST = dt.timezone(dt.timedelta(hours=9))
BAR = dt.timedelta(minutes=5)


def completed(df: pd.DataFrame, now: dt.datetime) -> pd.DataFrame:
    """足の開始時刻 + 5分 が now 以前のものだけ（まだ形成中の足は使わない）。"""
    return df[df.index + BAR <= now]


class ReplayFeed:
    """保存済みの過去データを、指定日の時刻に合わせて少しずつ見せる（動作確認用）。"""

    def __init__(self, date: str):
        self.date = dt.date.fromisoformat(date)
        self._cache: dict[str, pd.DataFrame] = {}

    def _day(self, ticker):
        if ticker not in self._cache:
            df = load(ticker)
            self._cache[ticker] = df[df.index.date == self.date]
        return self._cache[ticker]

    def bars(self, ticker: str, now: dt.datetime) -> pd.DataFrame:
        return completed(self._day(ticker), now)

    def clock(self):
        """その日の足の終わりの時刻を順に返す（リプレイの時計）。"""
        times = sorted({t for tk in self._cache for t in self._cache[tk].index})
        return [t + BAR for t in times]


class YahooFeed:
    """Yahoo Finance の5分足。無料だが日本株は遅れて届くことがあるため、仮想の売買用。
    本番では立花証券の時価に置き換える。"""

    def __init__(self):
        import yfinance  # noqa: F401  (使うときだけ読み込む)

    def bars(self, ticker: str, now: dt.datetime) -> pd.DataFrame:
        import yfinance as yf
        raw = yf.download(ticker, period="1d", interval="5m", progress=False, auto_adjust=False)
        if raw.empty:
            return raw
        raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
        df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index, utc=True).tz_convert("Asia/Tokyo")
        df = df[(df.index.date == now.date()) & (df["Volume"] > 0)]
        return completed(df, now)
