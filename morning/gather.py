"""朝の資料づくり。Claude が読むのは、ここで作る短い資料だけ（プランの利用枠の節約のため）。

2段階で作る（Claude が読む量を減らすため）:
  python -m morning.gather                    ① 全上場企業の開示の「題名一覧」を morning/briefs/<日付>.md に作る
  python -m morning.gather --detail 6976,3436 ② 選んだ銘柄（最大15社）の本文の冒頭を <日付>.detail.md に作る
  python -m morning.gather --date 2026-11-06  過去の日で試す（--detail と組み合わせてもよい）

① の対象: 前の営業日 15:25 より後〜今朝までの開示のうち、株価を動かしやすい種類（決算、業績修正、配当、
   自社株買い、TOB、株式分割、提携、受注、承認 など）。
   売買できる銘柄だけ: 100株を資金（config.toml の capital）で買えて、過去20日の平均売買代金が3,000万円以上
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
MIN_TURNOVER = 3e7
MAX_DETAIL = 15
TEXT_CHARS = 1800
# 題名にこれらを含む開示を対象にする（KINDS に加えて）
EXTRA_KINDS = ("株式分割", "公開買付", "提携", "受注", "承認", "月次", "増配", "特別配当", "株主優待",
               "上方修正", "下方修正", "新製品", "買収", "子会社化", "特許", "契約締結", "治験")
# 定例の報告など、株価をほとんど動かさない開示は除く
NOISE = ("訂正", "取得状況", "取得結果", "取得終了", "消却")
# 本文を読むときに優先する開示（先頭ほど優先）
DOC_PRIORITY = ("業績予想", "業績修正", "上方修正", "下方修正", "公開買付", "決算短信", "配当", "自己株式")


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
            if (lo < t <= hi and any(k in title for k in KINDS + EXTRA_KINDS)
                    and not any(k in title for k in NOISE)):
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


def _capital() -> int:
    import tomllib
    return int(tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))["money"]["capital"])


def build(d: dt.date, now: dt.datetime) -> Path | None:
    """① 題名一覧。"""
    if not is_trading_day(d):
        print(f"{d} は休場日です。資料は作りません。")
        return None
    cap = _capital()
    disc = disclosures(d, now)
    cands = pd.DataFrame()
    if len(disc):
        liq = liquidity(sorted(disc["code"].unique()), d)
        if len(liq):
            g = disc.groupby("code").agg(name=("name", "first"), titles=("title", " / ".join),
                                          urls=("url", list), pubdate=("pubdate", "min")).reset_index()
            cands = g.merge(liq, on="code")
            cands = cands[(cands["turnover"] >= MIN_TURNOVER) & (cands["prev_close"] * 100 <= cap)]
            cands = cands.sort_values("turnover", ascending=False)
    lines = [f"# {d} 朝の資料①: 開示の題名一覧（作成 {now:%H:%M}）", "",
             "## 前夜の海外市場と為替", *market_lines(), "",
             f"## 開示（前の営業日 15:25 〜 今朝）: {len(cands)}社",
             f"条件: 100株を{cap / 10000:.0f}万円以内で買える / 過去20日の平均売買代金 {MIN_TURNOVER / 1e4:,.0f}万円以上。売買代金の多い順",
             "形式: コード 社名 | 前日終値（100株の金額）| 平均売買代金 | 開示の題名", ""]
    for r in cands.itertuples():
        titles = r.titles if len(r.titles) <= 140 else r.titles[:140] + "…"
        lines.append(f"- {r.code} {r.name} | {r.prev_close:,.0f}円（{r.prev_close * 100 / 10000:,.1f}万円）"
                     f" | {r.turnover / 1e8:,.1f}億円 | {titles}")
    BRIEFS.mkdir(parents=True, exist_ok=True)
    path = BRIEFS / f"{d}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (BRIEFS / f"{d}.candidates.json").write_text(
        cands.drop(columns=["urls"]).to_json(orient="records", force_ascii=False), encoding="utf-8")
    (BRIEFS / f"{d}.urls.json").write_text(
        json.dumps(dict(zip(cands["code"], cands["urls"])) if len(cands) else {}, ensure_ascii=False), encoding="utf-8")
    print(f"資料①: {path}（{len(cands)}社、{len(path.read_text()) / 1000:.0f}千文字）")
    return path


def detail(d: dt.date, codes: list[str]) -> Path:
    """② 選んだ銘柄の本文の冒頭。"""
    codes = codes[:MAX_DETAIL]
    cands = {c["code"]: c for c in json.loads((BRIEFS / f"{d}.candidates.json").read_text(encoding="utf-8"))}
    urls = json.loads((BRIEFS / f"{d}.urls.json").read_text(encoding="utf-8"))
    lines = [f"# {d} 朝の資料②: 本文の冒頭（{len(codes)}社）", ""]
    for code in codes:
        if code not in cands:
            lines += [f"## {code}: 資料①の候補にない", ""]
            continue
        c = cands[code]
        us = urls.get(code, [])
        titles = c["titles"].split(" / ")
        order = sorted(range(len(us)), key=lambda i: next(
            (k for k, w in enumerate(DOC_PRIORITY) if w in titles[i]), len(DOC_PRIORITY)) if i < len(titles) else 99)
        lines += [f"## {code} {c['name']}", f"- 開示: {c['titles']}（{c['pubdate']}）",
                  f"- 前日終値 {c['prev_close']:,.1f}円 / 平均売買代金 {c['turnover'] / 1e8:,.1f}億円"]
        for i in order[:2]:
            lines.append(f"- 本文（{titles[i] if i < len(titles) else ''}）: {pdf_head(us[i])}")
        lines.append("")
    path = BRIEFS / f"{d}.detail.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"資料②: {path}（{len(codes)}社、{len(path.read_text()) / 1000:.0f}千文字）")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--detail", help="本文を読む銘柄コード（カンマ区切り、最大15社）")
    a = ap.parse_args()
    now = dt.datetime.now(JST)
    if a.date:
        d = dt.date.fromisoformat(a.date)
        now = dt.datetime.combine(d, dt.time(8, 0), JST)
    else:
        d = now.date()
    if a.detail:
        detail(d, [c.strip() for c in a.detail.split(",") if c.strip()])
    else:
        build(d, now)


if __name__ == "__main__":
    main()
