"""Claude の判断で売買する戦略（仮想の売買で検証中）。

9:52 ごろの「寄り付き後の見直し」（picks/<日付>.open.json）で keep（残す）になった銘柄を、
見直しが保存された直後の5分足で買う。損切りは「1回の損失の上限」に収まる幅（1%〜3%）に置き、
損切りに掛からなければ 15:20 に決済する。

見直しのファイルは、ボットの実行中に Claude が GitHub に保存する。ボットは作業中のファイルに触れずに
`git show origin/main:<path>` で読む（記録の書き込みとぶつからないように）。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_STOP, MAX_STOP = 0.01, 0.03


FADE_PREV = 0.10   # 前日の上昇率がこれ以上
FADE_GAP = 0.05    # さらに、今日の寄り付きがこれ以上の上昇で始まった
FADE_STOP = 0.08   # 損切りの幅（空売りなので、売値から8%上がったら買い戻す）
FADE_LAST = "09:30"  # この時刻までに条件を満たしたものだけ売る


class PicksStrategy:
    name = "Claudeの注目銘柄"
    trail = None

    def __init__(self, date: str, max_loss: int, use_git: bool = True, fixed_stop: float | None = None):
        self.fixed_stop = fixed_stop   # 研究用: 決まった幅で損切りする
        self.date = date
        self.max_loss = max_loss
        self.use_git = use_git
        self.keep: dict[str, dict] = {}   # ticker -> {decided_at, confidence, reason}
        self.fade: dict[str, dict] = {}   # 研究用の空売り候補 ticker -> {prev_close, prev_ret}
        self._fade_loaded = False

    # ---------- 見直しの読み込み
    def _read(self) -> dict | None:
        rel = f"picks/{self.date}.open.json"
        if self.use_git:
            try:
                subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=ROOT, timeout=60, check=True)
                out = subprocess.run(["git", "show", f"origin/main:{rel}"], cwd=ROOT, timeout=30,
                                     capture_output=True, text=True)
                if out.returncode == 0:
                    return json.loads(out.stdout)
            except Exception:  # noqa: BLE001
                pass
        p = ROOT / rel
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def _read_morning(self) -> dict | None:
        rel = f"picks/{self.date}.json"
        if self.use_git:
            out = subprocess.run(["git", "show", f"origin/main:{rel}"], cwd=ROOT, timeout=30,
                                 capture_output=True, text=True)
            if out.returncode == 0:
                return json.loads(out.stdout)
        p = ROOT / rel
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def refresh(self, now_hm: str = "") -> list[str]:
        """新しく売買の対象になった銘柄を返す。
        通常: 見直しで keep（残す）になった銘柄。
        研究用（fixed_stop あり）: 朝の買い候補を全部（見直しで外した銘柄も、印を付けて買う）。
          見直しが 10:30 までに届かなければ、朝の買い候補をそのまま買う。"""
        new = []
        if self.fixed_stop and not self._fade_loaded:
            new += self._load_fade()
        if self.keep:
            return new
        d = self._read()
        if self.fixed_stop:
            m = self._read_morning() if (d or now_hm >= "10:30") else None
            if not m:
                return new
            decisions = {r["code"]: r for r in (d or {}).get("reviews", [])}
            at = (d or {}).get("decided_at", "10:30")
            for p in m.get("picks", []):
                if p.get("action") != "long":
                    continue
                r = decisions.get(p["code"], {})
                self.keep[f"{p['code']}.T"] = {"decided_at": at, "decision": r.get("decision", "none"),
                                               "confidence": r.get("confidence", p.get("confidence")),
                                               "reason": r.get("reason", p.get("reason"))}
            return new + list(self.keep)
        if not d:
            return []
        for r in d.get("reviews", []):
            if r.get("decision") == "keep":
                self.keep[f"{r['code']}.T"] = {"decided_at": d.get("decided_at", "09:52"), "decision": "keep",
                                               "confidence": r.get("confidence"), "reason": r.get("reason")}
        return list(self.keep)

    def _load_fade(self) -> list[str]:
        """朝の資料から、前日に+10%以上上がった銘柄を読む（過去2年の分析で、寄り付きでさらに+5%以上
        上がって始まった日は、その後下がりやすかった: 9:05に空売りで平均+3.5%、損切り8%込み）。"""
        rel = f"morning/briefs/{self.date}.candidates.json"
        text = None
        if self.use_git:
            out = subprocess.run(["git", "show", f"origin/main:{rel}"], cwd=ROOT, timeout=30,
                                 capture_output=True, text=True)
            text = out.stdout if out.returncode == 0 else None
        elif (ROOT / rel).exists():
            text = (ROOT / rel).read_text(encoding="utf-8")
        if text is None:
            return []
        self._fade_loaded = True
        import re
        for c in json.loads(text):
            m = re.search(r"（開示なし）前日 ([+-][0-9.]+)%", c.get("titles", ""))
            if m and float(m.group(1)) / 100 >= FADE_PREV:
                self.fade[f"{c['code']}.T"] = {"prev_close": c["prev_close"], "prev_ret": float(m.group(1)) / 100,
                                               "decision": "fade"}
        return list(self.fade)

    def tickers(self) -> list[str]:
        return list(self.keep)

    # ---------- エンジンから呼ばれる部分（backtest.py の戦略と同じ形）
    def prepare(self, day):
        return {"close": day["Close"].to_numpy(), "times": day.index.strftime("%H:%M"),
                "open0": float(day["Open"].iloc[0])}

    def entry_signal(self, ctx, i, tm, ticker: str | None = None):
        f = self.fade.get(ticker) if ticker else None
        if f is not None and ticker not in self.keep:
            # 寄り付き（今日の最初の足の始値）が前日終値より+5%以上で始まり、まだ9:30前なら空売り
            if tm > FADE_LAST:
                return None
            gap = ctx["open0"] / f["prev_close"] - 1
            return (-1, FADE_STOP, None) if gap >= FADE_GAP else None
        k = self.keep.get(ticker) if ticker else None
        if k is None or tm < k["decided_at"]:
            return None
        price = float(ctx["close"][i])
        if self.fixed_stop:
            return (1, self.fixed_stop, None)
        # 100株で損切りに掛かったときの損失が上限に収まる幅。1%未満になる（株価が高すぎる）なら買えない
        stop = min(MAX_STOP, self.max_loss / (price * 100) * 0.995)   # 計算の誤差で99株にならないよう少し内側に
        if stop < MIN_STOP:
            stop = MIN_STOP   # risk.check が「上限の範囲で100株を買えない」として見送る
        return (1, stop, None)

    def exit_signal(self, ctx, i, side):
        return False
