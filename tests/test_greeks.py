import math

import pytest

from optionsagent.greeks import bs_price, compute_greeks, implied_vol, intrinsic, years_to_expiry

S, K, T, R, SIG = 100.0, 100.0, 0.25, 0.04, 0.30


def test_put_call_parity():
    call = bs_price(S, K, T, R, SIG, 0.0, "call")
    put = bs_price(S, K, T, R, SIG, 0.0, "put")
    assert call - put == pytest.approx(S - K * math.exp(-R * T), abs=1e-8)


def test_delta_bounds_and_sign():
    call = compute_greeks(S, K, T, R, SIG, 0.0, "call")
    put = compute_greeks(S, K, T, R, SIG, 0.0, "put")
    assert 0.0 < call.delta < 1.0
    assert -1.0 < put.delta < 0.0
    # Calls and puts on the same strike differ by one unit of delta.
    assert call.delta - put.delta == pytest.approx(1.0, abs=1e-6)


def test_gamma_and_vega_are_shared():
    call = compute_greeks(S, K, T, R, SIG, 0.0, "call")
    put = compute_greeks(S, K, T, R, SIG, 0.0, "put")
    assert call.gamma == pytest.approx(put.gamma, rel=1e-9)
    assert call.vega == pytest.approx(put.vega, rel=1e-9)


def test_long_options_decay():
    g = compute_greeks(S, K, T, R, SIG, 0.0, "call")
    assert g.theta < 0
    assert 0 < g.theta_pct_per_day < 0.1


def test_theta_accelerates_near_expiry():
    far = compute_greeks(S, K, years_to_expiry(45), R, SIG, 0.0, "call")
    near = compute_greeks(S, K, years_to_expiry(7), R, SIG, 0.0, "call")
    # The whole reason the agent buys 30-45 DTE instead of two weeks out.
    assert near.theta_pct_per_day > far.theta_pct_per_day * 2


def test_implied_vol_round_trip():
    for sigma in (0.12, 0.30, 0.85):
        for right in ("call", "put"):
            price = bs_price(S, K * 1.05, T, R, sigma, 0.0, right)
            solved = implied_vol(price, S, K * 1.05, T, R, 0.0, right)
            assert solved == pytest.approx(sigma, abs=1e-4)


def test_implied_vol_rejects_arbitrage_prices():
    # Below intrinsic: a crossed or stale quote, which must not produce a number.
    assert implied_vol(0.5, 150.0, 100.0, T, R, 0.0, "call") is None
    assert implied_vol(0.0, S, K, T, R, 0.0, "call") is None


def test_expired_option_is_intrinsic():
    assert bs_price(120.0, 100.0, 0.0, R, SIG, 0.0, "call") == pytest.approx(20.0)
    assert intrinsic(80.0, 100.0, "put") == pytest.approx(20.0)
    assert intrinsic(120.0, 100.0, "put") == 0.0
