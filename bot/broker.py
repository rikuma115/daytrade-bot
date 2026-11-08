"""発注の窓口。エンジンはこのインターフェースだけを使うので、発注先を差し替えられる。

本番で一番大事な点: 損切りは「逆指値注文」として証券会社のサーバーに置く。
ボットやサーバーが止まっても、損切りは取引所側で実行される。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class Position:
    ticker: str
    side: int          # 1=買い, -1=空売り
    qty: int
    entry_price: float
    entry_time: str
    stop: float
    target: float | None = None


@dataclass
class Fill:
    ticker: str
    kind: str          # entry / exit
    side: int
    qty: int
    price: float
    time: str
    reason: str


class Broker:
    def enter(self, ticker: str, side: int, qty: int, stop_pct: float, target_pct: float | None) -> None:
        """成行で新規注文。約定したら、約定価格から stop_pct 離れた位置に逆指値の損切りを置く。"""
        raise NotImplementedError

    def move_stop(self, ticker: str, price: float) -> None:
        raise NotImplementedError

    def exit(self, ticker: str, reason: str) -> None:
        """成行で決済（損切りの逆指値は取り消す）。"""
        raise NotImplementedError

    def positions(self) -> dict[str, Position]:
        raise NotImplementedError

    def pending(self) -> set[str]:
        """注文を出したが、まだ約定していない銘柄。"""
        raise NotImplementedError

    def sync(self, ticker: str, bar) -> list[Fill]:
        """新しく確定した足を受け取り、その間に起きた約定を返す。"""
        raise NotImplementedError


class PaperBroker(Broker):
    """仮想の売買。backtest.py と同じ約定ルールで、足のデータから約定を判定する。
    - 成行の注文は、次の足の始値で約定
    - 損切りは足の安値（空売りは高値）で判定。窓を開けて越えた場合は始値で約定
    """

    def __init__(self, force_exit: str = "15:20"):
        self.force_exit = force_exit
        self._pos: dict[str, Position] = {}
        self._orders: dict[str, dict] = {}   # ticker -> 次の足で執行する成行注文

    def enter(self, ticker, side, qty, stop_pct, target_pct):
        self._orders[ticker] = {"kind": "entry", "side": side, "qty": qty,
                                "stop_pct": stop_pct, "target_pct": target_pct}

    def move_stop(self, ticker, price):
        p = self._pos[ticker]
        p.stop = max(p.stop, price) if p.side == 1 else min(p.stop, price)

    def exit(self, ticker, reason):
        if ticker in self._pos:
            self._orders[ticker] = {"kind": "exit", "reason": reason}
        else:
            self._orders.pop(ticker, None)

    def positions(self):
        return dict(self._pos)

    def pending(self):
        return {t for t, o in self._orders.items() if o["kind"] == "entry"}

    def sync(self, ticker, bar):
        fills = []
        tm = bar.name.strftime("%H:%M")
        o, h, l = float(bar["Open"]), float(bar["High"]), float(bar["Low"])
        order = self._orders.pop(ticker, None)
        if order and order["kind"] == "entry" and ticker not in self._pos:
            side, ep = order["side"], o
            stop = ep * (1 - side * order["stop_pct"])
            tgt = ep * (1 + side * order["target_pct"]) if order["target_pct"] else None
            self._pos[ticker] = Position(ticker, side, order["qty"], ep, tm, stop, tgt)
            fills.append(Fill(ticker, "entry", side, order["qty"], ep, tm, "新規"))
        elif order and order["kind"] == "exit" and ticker in self._pos:
            p = self._pos.pop(ticker)
            fills.append(Fill(ticker, "exit", p.side, p.qty, o, tm, order["reason"]))
            return fills
        p = self._pos.get(ticker)
        if p is None:
            return fills
        if tm >= self.force_exit:
            # 決済時刻を過ぎた足（直前の足が欠けていた場合など）。時刻で決済が出ているはずなので始値で決済
            self._pos.pop(ticker)
            fills.append(Fill(ticker, "exit", p.side, p.qty, o, tm, "大引け前決済"))
            return fills
        hit_stop = l <= p.stop if p.side == 1 else h >= p.stop
        hit_tgt = p.target is not None and (h >= p.target if p.side == 1 else l <= p.target)
        if hit_stop:
            px = min(o, p.stop) if p.side == 1 else max(o, p.stop)
            self._pos.pop(ticker)
            fills.append(Fill(ticker, "exit", p.side, p.qty, px, tm, "損切り"))
        elif hit_tgt:
            px = max(o, p.target) if p.side == 1 else min(o, p.target)
            self._pos.pop(ticker)
            fills.append(Fill(ticker, "exit", p.side, p.qty, px, tm, "利確"))
        return fills

    def close_now(self, ticker, price, time, reason):
        """その日のデータが尽きたときの最後の手段（仮想の売買のみ）。"""
        p = self._pos.pop(ticker)
        self._orders.pop(ticker, None)
        return Fill(ticker, "exit", p.side, p.qty, price, time, reason)

    # 再起動しても状態を引き継ぐための保存と復元
    def dump(self):
        return {"pos": {t: asdict(p) for t, p in self._pos.items()}, "orders": self._orders}

    def restore(self, d):
        self._pos = {t: Position(**p) for t, p in d.get("pos", {}).items()}
        self._orders = d.get("orders", {})


class TachibanaBroker(Broker):
    """立花証券 e支店 API（口座開設後に実装する）。

    実装の方針:
    - enter: 成行の新規注文 → 約定通知を受けたら、約定価格を基準に逆指値（損切り）を発注
    - move_stop: 逆指値の訂正注文（有利な方向にだけ動かす）
    - exit: 逆指値を取り消してから成行で決済。取り消しに失敗したら決済しない（二重売りを防ぐ）
    - sync: 注文と約定の照会APIで、ボットの認識と証券会社側の状態を毎回照合する
    仕様書とAPIの利用条件は、口座開設後に e支店のサイトで確認する。
    """

    def __init__(self, *_, **__):
        raise NotImplementedError("立花証券 e支店 API は口座開設後に実装します。今は mode = \"paper\" で動かしてください。")
