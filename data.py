"""株価データ（5分足）の取得と保存。

Yahoo Finance の 5分足は直近60日分までしか取れないので、毎日取りに行って貯めていく。
保存先は data/5m/<日付>.csv（1日1ファイル、全銘柄）。過去の日のファイルは書き換えない
（GitHub のリポジトリが毎日大きく膨らまないようにするため）。

  python3 data.py            全銘柄の直近60日分を取得し、まだない日のファイルを追加する
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data" / "5m"

# 監視する銘柄（売買が活発なもの）。実際に売買するのは、この中から資金と流動性で絞った銘柄
UNIVERSE = {
    # 銀行・金融
    "8306.T": "三菱UFJ", "8316.T": "三井住友FG", "8411.T": "みずほFG", "8604.T": "野村HD",
    "8591.T": "オリックス", "8766.T": "東京海上", "8725.T": "MS&AD", "8630.T": "SOMPO",
    "8750.T": "第一生命", "7182.T": "ゆうちょ銀", "6178.T": "日本郵政", "8308.T": "りそな",
    "8309.T": "三井住友トラスト", "7186.T": "コンコルディア",
    # 自動車
    "7203.T": "トヨタ", "7267.T": "ホンダ", "7201.T": "日産", "7211.T": "三菱自動車",
    "7261.T": "マツダ", "7269.T": "スズキ", "7270.T": "SUBARU", "6902.T": "デンソー",
    # 電機・半導体
    "6758.T": "ソニーG", "6501.T": "日立", "6702.T": "富士通", "6752.T": "パナソニック",
    "6503.T": "三菱電機", "6701.T": "NEC", "8035.T": "東京エレクトロン", "6857.T": "アドバンテスト",
    "6920.T": "レーザーテック", "6146.T": "ディスコ", "7735.T": "SCREEN", "6723.T": "ルネサス",
    "6526.T": "ソシオネクスト", "6963.T": "ローム", "6981.T": "村田製作所", "6976.T": "太陽誘電",
    "6762.T": "TDK", "4062.T": "イビデン", "3436.T": "SUMCO", "6954.T": "ファナック",
    "7751.T": "キヤノン", "4063.T": "信越化学",
    # 商社
    "8058.T": "三菱商事", "8031.T": "三井物産", "8001.T": "伊藤忠", "8053.T": "住友商事",
    "8002.T": "丸紅", "2768.T": "双日",
    # 通信・ネット
    "9432.T": "NTT", "9434.T": "ソフトバンク", "9433.T": "KDDI", "9984.T": "ソフトバンクG",
    "4755.T": "楽天G",
    # 機械・重工
    "7011.T": "三菱重工", "7012.T": "川崎重工", "7013.T": "IHI", "6301.T": "コマツ", "6367.T": "ダイキン",
    # 資源・素材
    "5020.T": "ENEOS", "1605.T": "INPEX", "5401.T": "日本製鉄", "5411.T": "JFE",
    "5713.T": "住友金属鉱山", "5801.T": "古河電工", "5803.T": "フジクラ", "4005.T": "住友化学",
    "3402.T": "東レ",
    # 海運・空運・鉄道
    "9101.T": "日本郵船", "9104.T": "商船三井", "9107.T": "川崎汽船", "9201.T": "JAL",
    "9202.T": "ANA", "9020.T": "JR東日本",
    # 医薬
    "4502.T": "武田薬品", "4568.T": "第一三共", "4503.T": "アステラス", "4519.T": "中外製薬",
    # 小売・サービス・その他
    "9983.T": "ファーストリテ", "7974.T": "任天堂", "6098.T": "リクルート", "4661.T": "OLC",
    "2914.T": "JT", "1925.T": "大和ハウス", "8801.T": "三井不動産", "8802.T": "三菱地所",
    "4307.T": "野村総研",
}


@lru_cache(maxsize=1)
def _all() -> pd.DataFrame:
    files = sorted(DATA_DIR.glob("20*.csv"))
    if not files:
        return pd.DataFrame(columns=["ticker", "Open", "High", "Low", "Close", "Volume"])
    df = pd.concat([pd.read_csv(f, index_col=0) for f in files])
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("Asia/Tokyo")
    return df.sort_index()


def load(ticker: str) -> pd.DataFrame:
    """保存済みの5分足（出来高0の足は除く）。"""
    df = _all()
    df = df[df["ticker"] == ticker][["Open", "High", "Low", "Close", "Volume"]]
    return df[df["Volume"] > 0]


def tickers_with_data() -> list[str]:
    return sorted(_all()["ticker"].unique())


def update(tickers=None, period="60d") -> int:
    """直近の5分足を取得して、まだない日のファイルを追加する（今日の分は書き直す）。追加した日数を返す。"""
    import yfinance as yf
    tickers = list(tickers or UNIVERSE)
    raw = yf.download(tickers, period=period, interval="5m", progress=False, auto_adjust=False,
                      group_by="ticker", threads=True)
    frames = []
    for t in tickers:
        if t not in raw.columns.get_level_values(0):
            continue
        df = raw[t][["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"]).copy()
        if df.empty:
            continue
        df.index = pd.to_datetime(df.index, utc=True).tz_convert("Asia/Tokyo")
        df.insert(0, "ticker", t)
        frames.append(df)
    if not frames:
        return 0
    allbars = pd.concat(frames).sort_index()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    added = 0
    for d, day in allbars.groupby(allbars.index.date):
        path = DATA_DIR / f"{d}.csv"
        day.index.name = "Datetime"
        if path.exists() and d < today:
            # 確定済みの日は、既にある銘柄を書き換えない。新しく監視に加えた銘柄だけ追記する
            have = set(pd.read_csv(path, usecols=["ticker"])["ticker"])
            new = day[~day["ticker"].isin(have)]
            if new.empty:
                continue
            old = pd.read_csv(path, index_col=0)
            new_out = new.copy()
            new_out.index = new_out.index.map(lambda x: x.isoformat(sep=" "))
            pd.concat([old, new_out]).to_csv(path)
        else:
            day.to_csv(path)
        added += 1
    _all.cache_clear()
    return added


if __name__ == "__main__":
    n = update()
    df = _all()
    print(f"追加・更新した日: {n} / 保存済み {df.index.normalize().nunique()}日分、{df['ticker'].nunique()}銘柄")
