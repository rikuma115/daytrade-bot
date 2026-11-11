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


class PicksStrategy:
    name = "Claudeの注目銘柄"
    trail = None

    def __init__(self, date: str, max_loss: int, use_git: bool = True):
        self.date = date
        self.max_loss = max_loss
        self.use_git = use_git
        self.keep: dict[str, dict] = {}   # ticker -> {decided_at, confidence, reason}

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

    def refresh(self) -> list[str]:
        """新しく「残す」になった銘柄を返す。"""
        if self.keep:
            return []
        d = self._read()
        if not d:
            return []
        for r in d.get("reviews", []):
            if r.get("decision") == "keep":
                self.keep[f"{r['code']}.T"] = {"decided_at": d.get("decided_at", "09:52"),
                                               "confidence": r.get("confidence"), "reason": r.get("reason")}
        return list(self.keep)

    def tickers(self) -> list[str]:
        return list(self.keep)

    # ---------- エンジンから呼ばれる部分（backtest.py の戦略と同じ形）
    def prepare(self, day):
        return {"close": day["Close"].to_numpy(), "times": day.index.strftime("%H:%M")}

    def entry_signal(self, ctx, i, tm, ticker: str | None = None):
        k = self.keep.get(ticker) if ticker else None
        if k is None or tm < k["decided_at"]:
            return None
        price = float(ctx["close"][i])
        # 100株で損切りに掛かったときの損失が上限に収まる幅。1%未満になる（株価が高すぎる）なら買えない
        stop = min(MAX_STOP, self.max_loss / (price * 100) * 0.995)   # 計算の誤差で99株にならないよう少し内側に
        if stop < MIN_STOP:
            stop = MIN_STOP   # risk.check が「上限の範囲で100株を買えない」として見送る
        return (1, stop, None)

    def exit_signal(self, ctx, i, side):
        return False
