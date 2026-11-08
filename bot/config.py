import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    mode: str
    account: str
    capital: int
    max_loss_per_trade: int
    daily_loss_limit: int
    max_trades_per_day: int
    max_positions: int
    slippage: float
    entry_start: str
    entry_end: str
    force_exit: str
    strategy: str
    strategy_params: dict
    tm_enabled: bool
    tm_block_above: float
    tm_replays: int
    tickers: list
    min_turnover: float
    notify_on_trade: bool
    notify_on_summary: bool
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def allow_short(self) -> bool:
        return self.account == "margin"


def _tickers(v):
    if v == "auto":
        from data import UNIVERSE
        return list(UNIVERSE)
    return list(v)


def load(path: str | Path = ROOT / "config.toml") -> Config:
    c = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    m, t, s, tm, u, n = (c.get(k, {}) for k in ("money", "time", "strategy", "timemachine", "universe", "notify"))
    cfg = Config(
        mode=c.get("mode", "paper"), account=c.get("account", "cash"),
        capital=int(m["capital"]), max_loss_per_trade=int(m["max_loss_per_trade"]),
        daily_loss_limit=int(m["daily_loss_limit"]), max_trades_per_day=int(m["max_trades_per_day"]),
        max_positions=int(m["max_positions"]), slippage=float(m.get("slippage", 0.0005)),
        entry_start=t["entry_start"], entry_end=t["entry_end"], force_exit=t["force_exit"],
        strategy=s["name"], strategy_params=s.get("params", {}),
        tm_enabled=bool(tm.get("enabled", True)), tm_block_above=float(tm.get("block_if_stop_first_above", 1.0)),
        tm_replays=int(tm.get("replays", 2000)),
        tickers=_tickers(u.get("tickers", "auto")),
        min_turnover=float(u.get("min_turnover", 3e9)),
        notify_on_trade=bool(n.get("on_trade", True)), notify_on_summary=bool(n.get("on_daily_summary", True)),
        raw=c,
    )
    if cfg.mode not in ("paper", "live"):
        raise ValueError("mode は paper か live")
    if cfg.account not in ("cash", "margin"):
        raise ValueError("account は cash か margin")
    if cfg.max_loss_per_trade <= 0 or cfg.daily_loss_limit <= 0:
        raise ValueError("損失の上限は正の数にする")
    return cfg
