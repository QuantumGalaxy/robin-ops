"""A rule-based options trading agent with Greeks-aware entry screening."""

__version__ = "0.1.0"

from .config import Config
from .engine import TradingEngine
from .portfolio import Portfolio

__all__ = ["Config", "Portfolio", "TradingEngine", "__version__"]
