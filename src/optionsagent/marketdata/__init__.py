from .base import MarketDataProvider
from .synthetic import SyntheticMarketData

__all__ = ["MarketDataProvider", "SyntheticMarketData", "build_provider"]


def build_provider(kind: str, **kwargs):
    """Factory used by the CLI so provider choice is a single config string."""
    if kind in ("paper", "synthetic"):
        return SyntheticMarketData(**kwargs)
    if kind == "robinhood":
        from .robinhood import RobinhoodMarketData

        return RobinhoodMarketData(**kwargs)
    raise ValueError(f"unknown market data provider: {kind}")
