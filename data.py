"""株価データの取得とキャッシュ。

Yahoo Finance の 5分足は直近60日分までしか取れない。
取得したデータは data/ に CSV で保存し、2回目以降はそれを使う。
"""
from pathlib import Path

import pandas as pd
import yfinance as yf

DATA_DIR = Path(__file__).parent / "data"

# 流動性の高い主要銘柄（デイトレードで滑りにくいもの）
UNIVERSE = {
    "7203.T": "トヨタ",
    "6758.T": "ソニーG",
    "8306.T": "三菱UFJ",
    "9984.T": "ソフトバンクG",
    "8035.T": "東京エレクトロン",
    "6501.T": "日立",
    "7011.T": "三菱重工",
    "8058.T": "三菱商事",
    "9432.T": "NTT",
    "8316.T": "三井住友FG",
    "6098.T": "リクルート",
    "7974.T": "任天堂",
    "4063.T": "信越化学",
    "6857.T": "アドバンテスト",
    "9983.T": "ファーストリテ",
    "8411.T": "みずほFG",
    "7267.T": "ホンダ",
    "4502.T": "武田薬品",
    "6702.T": "富士通",
    "5401.T": "日本製鉄",
}


def load(ticker: str, interval: str = "5m", period: str = "60d", refresh: bool = False) -> pd.DataFrame:
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{ticker}_{interval}.csv"
    if path.exists() and not refresh:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
    else:
        raw = yf.download(ticker, period=period, interval=interval,
                          progress=False, auto_adjust=False)
        if raw.empty:
            raise RuntimeError(f"{ticker}: データを取得できませんでした")
        raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
        df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
        # 既存のキャッシュがあれば結合して、60日より古いデータも貯めていく
        if path.exists():
            old = pd.read_csv(path, index_col=0, parse_dates=True)
            df = pd.concat([old, df])
            df = df[~df.index.duplicated(keep="last")]
        df.to_csv(path)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("Asia/Tokyo")
    df = df.sort_index()
    df = df[df["Volume"] > 0]
    return df


if __name__ == "__main__":
    for t, name in UNIVERSE.items():
        d = load(t, refresh=True)
        print(f"{t} {name}: {len(d)}本 {d.index[0].date()}〜{d.index[-1].date()}")
