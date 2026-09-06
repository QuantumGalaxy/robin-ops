from .base import Broker
from .paper import PaperBroker

__all__ = ["Broker", "PaperBroker", "build_broker"]


def build_broker(kind: str, **kwargs) -> Broker:
    if kind == "paper":
        return PaperBroker(**kwargs)
    if kind in ("robinhood_mcp", "robinhood-mcp"):
        from ..mcp.client import HttpToolCaller
        from .robinhood_mcp import RobinhoodMcpBroker

        caller = kwargs.pop("caller", None) or HttpToolCaller()
        return RobinhoodMcpBroker(caller=caller, **kwargs)
    if kind == "robinhood":
        # Unofficial robin_stocks path. Prefer robinhood_mcp.
        from .robinhood import RobinhoodBroker

        return RobinhoodBroker(**kwargs)
    raise ValueError(f"unknown broker: {kind}")
