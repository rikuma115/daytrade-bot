"""株数の計算と、注文前のリスクチェック。ここを通らない注文は出さない。"""
from __future__ import annotations

from dataclasses import dataclass

LOT = 100   # 日本株の売買単位


@dataclass
class Decision:
    ok: bool
    qty: int
    reason: str


def size(price: float, stop_pct: float, max_loss: int, capital: int) -> int:
    """損切りに掛かったときの損失が max_loss 以下、かつ資金内に収まる株数（100株単位）。"""
    risk_per_share = price * stop_pct
    by_risk = int(max_loss // risk_per_share) // LOT * LOT if risk_per_share > 0 else 0
    by_cash = int(capital // price) // LOT * LOT
    return max(0, min(by_risk, by_cash))


def check(cfg, *, ticker: str, side: int, price: float, stop_pct: float, tm: str,
          state, open_positions: dict, pending: set) -> Decision:
    """新規注文を出してよいか。だめなら理由を返す。"""
    if state.halted:
        return Decision(False, 0, f"本日停止中（{state.halt_reason}）")
    if side == -1 and not cfg.allow_short:
        return Decision(False, 0, "現物口座では空売りしない")
    if not (cfg.entry_start <= tm < cfg.entry_end):
        return Decision(False, 0, "新規注文の時間外")
    if ticker in open_positions or ticker in pending:
        return Decision(False, 0, "同じ銘柄を保有中か注文中")
    if cfg.account == "cash" and ticker in state.traded_today:
        return Decision(False, 0, "現物は1銘柄1日1往復まで（差金決済の禁止）")
    if state.entries >= cfg.max_trades_per_day:
        return Decision(False, 0, "1日の取引回数の上限")
    if len(open_positions) + len(pending) >= cfg.max_positions:
        return Decision(False, 0, "同時に持てる銘柄数の上限")
    # 今の損失 + 保有中の建玉が全て損切りになった場合の損失 + この注文の最大損失 が上限を超えないか
    worst_open = sum(abs(p.entry_price - p.stop) * p.qty for p in open_positions.values())
    room = cfg.daily_loss_limit + min(state.realized, 0) - worst_open
    if room <= 0:
        return Decision(False, 0, "1日の損失の上限に余裕がない")
    used = sum(p.entry_price * p.qty for p in open_positions.values())
    cash = min(cfg.capital - used, cfg.max_per_position)   # 1銘柄に使うお金の上限（分散のため）
    qty = size(price, stop_pct, min(cfg.max_loss_per_trade, room), cash)
    if qty <= 0:
        return Decision(False, 0, "資金か損失の上限の範囲で100株を買えない")
    return Decision(True, qty, "OK")
