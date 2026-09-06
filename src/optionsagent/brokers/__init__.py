from .base import Broker
from .paper import PaperBroker

__all__ = ["Broker", "PaperBroker", "build_broker"]


def build_broker(kind: str, **kwargs) -> Broker:
    if kind == "paper":
        return PaperBroker(**kwargs)
    if kind == "robinhood":
        from .robinhood import RobinhoodBroker

        return RobinhoodBroker(**kwargs)
    raise ValueError(f"unknown broker: {kind}")
