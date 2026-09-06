from .base import MarketDataProvider
from .synthetic import SyntheticMarketData

__all__ = ["MarketDataProvider", "SyntheticMarketData", "build_provider"]


def build_provider(kind: str, **kwargs):
    """Factory used by the CLI so provider choice is a single config string."""
    if kind in ("paper", "synthetic"):
        return SyntheticMarketData(**kwargs)
    if kind in ("robinhood_mcp", "robinhood-mcp"):
        from ..mcp.client import HttpToolCaller
        from .robinhood_mcp import RobinhoodMcpMarketData

        caller = kwargs.pop("caller", None) or HttpToolCaller()
        return RobinhoodMcpMarketData(caller=caller, **kwargs)
    if kind == "robinhood":
        # Unofficial robin_stocks path. Prefer robinhood_mcp.
        from .robinhood import RobinhoodMarketData

        return RobinhoodMarketData(**kwargs)
    raise ValueError(f"unknown market data provider: {kind}")
