import math
from datetime import date, timedelta

import pytest

from optionsagent.config import EntryConfig, SizingConfig
from optionsagent.greeks import compute_greeks, years_to_expiry
from optionsagent.models import OptionContract, OptionQuote
from optionsagent.strategy.entry import EntryScreener, move_in_sigmas, required_underlying_move
from optionsagent.strategy.sizing import size_position

TODAY = date(2025, 6, 2)


def build_quote(
    strike: float = 195.0,
    spot: float = 200.0,
    dte: int = 35,
    iv: float = 0.28,
    spread_pct: float = 0.02,
    oi: int = 5000,
    right: str = "call",
) -> OptionQuote:
    contract = OptionContract("AAPL", TODAY + timedelta(days=dte), strike, right)
    g = compute_greeks(spot, strike, years_to_expiry(dte), 0.04, iv, 0.0, right)
    half = g.price * spread_pct / 2
    return OptionQuote(
        contract=contract,
        bid=round(g.price - half, 2),
        ask=round(g.price + half, 2),
        underlying_price=spot,
        open_interest=oi,
        volume=int(oi * 0.1),
        greeks=g,
    )


def screener(**overrides) -> EntryScreener:
    overrides = {"min_dte": 30, "max_dte": 45, **overrides}
    return EntryScreener(cfg=EntryConfig(**overrides), target_return=0.10, horizon_days=14.0)


def test_in_the_money_needs_a_smaller_move_than_out_of_the_money():
    itm = required_underlying_move(build_quote(strike=185.0), 0.10, 14.0, 35)
    otm = required_underlying_move(build_quote(strike=225.0), 0.10, 14.0, 35)
    assert itm < otm


def test_short_dated_contracts_need_a_bigger_move_for_the_same_target():
    # Decay has to be overcome before delta can deliver the gain, so a two-week
    # contract asks far more of the stock than a six-week one.
    far = required_underlying_move(build_quote(strike=200.0, dte=40), 0.10, 14.0, 40)
    near = required_underlying_move(build_quote(strike=208.0, dte=14, iv=0.35), 0.10, 14.0, 14)
    assert near > far * 2


def test_required_move_is_infinite_when_the_target_is_unreachable():
    # A far out-of-the-money contract held past its own expiry cannot get there.
    q = build_quote(strike=400.0, dte=3, iv=0.25)
    assert math.isinf(required_underlying_move(q, 0.10, 14.0, 3))


def test_puts_require_a_downward_move():
    q = build_quote(strike=205.0, right="put")
    move = required_underlying_move(q, 0.10, 14.0, 35)
    assert 0.0 < move < 0.5


def test_move_in_sigmas_scales_with_volatility():
    calm = build_quote(iv=0.15)
    wild = build_quote(iv=0.60)
    move = 0.03
    assert move_in_sigmas(calm, move, 14.0) > move_in_sigmas(wild, move, 14.0)


def test_screener_accepts_a_reasonable_contract():
    out = screener().screen([build_quote()], "call", as_of=TODAY, iv_rank=0.3)
    assert len(out) == 1
    assert 0.45 <= abs(out[0].delta) <= 0.70


def test_screener_rejects_wide_spreads():
    assert screener().screen([build_quote(spread_pct=0.25)], "call", as_of=TODAY, iv_rank=0.3) == []


def test_screener_rejects_illiquid_contracts():
    assert screener().screen([build_quote(oi=10)], "call", as_of=TODAY, iv_rank=0.3) == []


def test_screener_rejects_wrong_direction():
    assert screener().screen([build_quote(right="put")], "call", as_of=TODAY, iv_rank=0.3) == []


def test_screener_rejects_short_dated_contracts():
    assert screener().screen([build_quote(dte=10)], "call", as_of=TODAY, iv_rank=0.3) == []


def test_screener_rejects_expensive_volatility():
    quotes = [build_quote()]
    assert screener().screen(quotes, "call", iv_rank=0.9, as_of=TODAY) == []
    assert screener().screen(quotes, "call", iv_rank=0.3, as_of=TODAY) != []


def test_screener_avoids_earnings():
    quotes = [build_quote()]
    assert screener().screen(quotes, "call", days_to_earnings=3, as_of=TODAY, iv_rank=0.3) == []
    assert screener().screen(quotes, "call", days_to_earnings=30, as_of=TODAY, iv_rank=0.3) != []


def test_screener_returns_nothing_without_a_direction():
    assert screener().screen([build_quote()], "none", as_of=TODAY, iv_rank=0.3) == []


def test_ranking_prefers_the_cheaper_spread():
    tight = build_quote(strike=195.0, spread_pct=0.01)
    wide = build_quote(strike=195.0, spread_pct=0.05)
    ranked = screener().screen([wide, tight], "call", as_of=TODAY, iv_rank=0.3)
    assert ranked[0].quote.spread_pct < ranked[-1].quote.spread_pct


def test_sizing_risks_the_configured_fraction():
    q = build_quote()
    cfg = SizingConfig(risk_per_trade_pct=0.02, max_contracts=50, max_trade_premium=5000)
    d = size_position(q, equity=100_000, buying_power=100_000, open_premium=0, cfg=cfg)
    # 2% of 100k risked against a 50% stop means about 4k of premium.
    assert d.risk_dollars == pytest.approx(2_000, rel=0.25)
    assert d.premium == pytest.approx(4_000, rel=0.25)


def test_sizing_respects_the_portfolio_premium_cap():
    q = build_quote()
    cfg = SizingConfig(max_portfolio_premium_pct=0.25)
    d = size_position(q, equity=100_000, buying_power=100_000, open_premium=25_000, cfg=cfg)
    assert d.contracts == 0
    assert "premium cap" in d.reason


def test_sizing_refuses_when_one_contract_is_too_expensive():
    q = build_quote(strike=100.0, spot=1000.0, iv=0.3)
    cfg = SizingConfig(risk_per_trade_pct=0.02)
    d = size_position(q, equity=10_000, buying_power=10_000, open_premium=0, cfg=cfg)
    assert d.contracts == 0
