"""朝の資料づくり。Claude が読むのは、ここで作る短い資料だけ（プランの利用枠の節約のため）。

  python -m morning.gather             今日の資料を morning/briefs/<日付>.md に作る
  python -m morning.gather --date 2026-11-06   過去の日で試す

資料の中身:
  1. 前夜の海外市場と為替（米国株、半導体指数、ドル円、日経平均先物）
  2. 前の営業日 15:25 より後〜今朝までの適時開示のうち、決算・業績修正・配当修正・自社株買い
     （売買しやすい銘柄だけ: 過去20日の平均売買代金が5億円以上）。PDF本文の冒頭つき
休場日（土日祝、年末年始）は資料を作らずに終了する。
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))
from exp2_disclosures import KINDS, _get, fetch_list  # noqa: E402

BRIEFS = ROOT / "morning" / "briefs"
JST = dt.timezone(dt.timedelta(hours=9))
MARKETS = {"^GSPC": "S&P500", "^IXIC": "ナスダック", "^SOX": "米半導体指数", "USDJPY=X": "ドル円",
           "NIY=F": "日経平均先物（CME円建て）", "^N225": "日経平均（前日終値）"}
MIN_TURNOVER = 5e8
MAX_CANDIDATES = 40
TEXT_CHARS = 1500


def is_trading_day(d: dt.date) -> bool:
    import jpholiday
    if d.weekday() >= 5 or jpholiday.is_holiday(d):
        return False
    return not ((d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3))


def prev_trading_day(d: dt.date) -> dt.date:
    p = d - dt.timedelta(days=1)
    while not is_trading_day(p):
        p -= dt.timedelta(days=1)
    return p


def market_lines() -> list[str]:
    import yfinance as yf
    out = []
    for t, name in MARKETS.items():
        try:
            d = yf.download(t, period="7d", interval="1d", progress=False, auto_adjust=False)
            d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
            c = d["Close"].dropna()
            out.append(f"- {name}: {c.iloc[-1]:,.2f}（前日比 {(c.iloc[-1] / c.iloc[-2] - 1) * 100:+.2f}%、{c.index[-1].date()}）")
        except Exception as e:  # noqa: BLE001
            out.append(f"- {name}: 取得できず（{type(e).__name__}）")
    return out


def disclosures(d: dt.date, now: dt.datetime) -> pd.DataFrame:
    prev = prev_trading_day(d)
    lo = dt.datetime.combine(prev, dt.time(15, 25))
    hi = min(now.replace(tzinfo=None), dt.datetime.combine(d, dt.time(8, 59)))
    rows, day = [], prev
    while day <= d:
        for x in fetch_list(day):
            t = dt.datetime.fromisoformat(x["pubdate"])
            title = x["title"]
            if lo < t <= hi and any(k in title for k in KINDS) and "訂正" not in title:
                rows.append({"code": x["company_code"][:4], "name": x["company_name"], "title": title,
                             "pubdate": x["pubdate"], "url": x["document_url"].split("rd.php?")[-1]})
        day += dt.timedelta(days=1)
    return pd.DataFrame(rows)


def liquidity(codes: list[str], d: dt.date) -> pd.DataFrame:
    import yfinance as yf
    if not codes:
        return pd.DataFrame()
    px = yf.download([f"{c}.T" for c in codes], period="2mo", interval="1d", progress=False,
                     auto_adjust=False, group_by="ticker", threads=True)
    rows = []
    for c in codes:
        t = f"{c}.T"
        if t not in px.columns.get_level_values(0):
            continue
        p = px[t].dropna(subset=["Close"])
        p = p[[i.date() < d for i in p.index]].tail(20)
        if len(p) < 10:
            continue
        rows.append({"code": c, "prev_close": float(p["Close"].iloc[-1]),
                     "turnover": float((p["Close"] * p["Volume"]).mean())})
    return pd.DataFrame(rows)


def pdf_head(url: str) -> str:
    from pypdf import PdfReader
    try:
        rd = PdfReader(io.BytesIO(_get(url)))
        txt = "\n".join((pg.extract_text() or "") for pg in rd.pages[:2])
        txt = " ".join(txt.split())
        # PDF から取り出すと日本語の文字の間に空白が入るので詰める（読む量を減らす）
        txt = re.sub(r"(?<=[^\x00-\x7f]) (?=[^\x00-\x7f])", "", txt)
        txt = re.sub(r"(?<=[^\x00-\x7f]) (?=[0-9,.%△▲()（）])|(?<=[0-9,.%]) (?=[^\x00-\x7f])", "", txt)
        return txt[:TEXT_CHARS]
    except Exception as e:  # noqa: BLE001
        return f"（本文を読めませんでした: {type(e).__name__}）"


def build(d: dt.date, now: dt.datetime) -> Path | None:
    if not is_trading_day(d):
        print(f"{d} は休場日です。資料は作りません。")
        return None
    disc = disclosures(d, now)
    cands = pd.DataFrame()
    if len(disc):
        liq = liquidity(sorted(disc["code"].unique()), d)
        if len(liq):
            g = disc.groupby("code").agg(name=("name", "first"), titles=("title", " / ".join),
                                          urls=("url", list), pubdate=("pubdate", "min")).reset_index()
            cands = g.merge(liq, on="code")
            cands = cands[cands["turnover"] >= MIN_TURNOVER].sort_values("turnover", ascending=False)
            cands = cands.head(MAX_CANDIDATES)
    lines = [f"# {d} 朝の資料（作成 {now:%H:%M}）", "", "## 前夜の海外市場と為替", *market_lines(), "",
             f"## 適時開示（前の営業日 15:25 〜 今朝。売買代金5億円以上、最大{MAX_CANDIDATES}社）",
             f"対象 {len(cands)}社（開示の総数 {len(disc)}件）", ""]
    for r in cands.itertuples():
        text = pdf_head(r.urls[0])
        lines += [f"### {r.code} {r.name}",
                  f"- 開示: {r.titles}（{r.pubdate}）",
                  f"- 前日終値 {r.prev_close:,.1f}円 / 平均売買代金 {r.turnover / 1e8:,.0f}億円",
                  f"- 本文の冒頭: {text}", ""]
    BRIEFS.mkdir(parents=True, exist_ok=True)
    path = BRIEFS / f"{d}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (BRIEFS / f"{d}.candidates.json").write_text(
        cands.drop(columns=["urls"]).to_json(orient="records", force_ascii=False), encoding="utf-8")
    print(f"資料: {path}（候補 {len(cands)}社、{len(path.read_text()) / 1000:.0f}千文字）")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    a = ap.parse_args()
    now = dt.datetime.now(JST)
    if a.date:
        d = dt.date.fromisoformat(a.date)
        now = dt.datetime.combine(d, dt.time(8, 0), JST)
    else:
        d = now.date()
    build(d, now)


if __name__ == "__main__":
    main()
