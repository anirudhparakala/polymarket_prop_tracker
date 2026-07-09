import math

import pytest

from fixtures import SCENARIOS, FixtureSource

WALLET = "0x" + "0" * 40


def _by_title(scenario: str) -> dict[str, object]:
    return {p.market_title: p for p in FixtureSource(scenario).fetch(WALLET)}


def test_scenarios_are_the_three_documented_ones():
    assert SCENARIOS == ("before_match", "after_goal", "after_cashout")


def test_unknown_scenario_is_rejected():
    with pytest.raises(ValueError, match="Unknown scenario"):
        FixtureSource("nonsense")


def test_before_match_has_three_props_each_worth_its_stake():
    positions = _by_title("before_match")
    assert len(positions) == 3
    for p in positions.values():
        assert math.isclose(p.current_value, p.stake, rel_tol=1e-9)
        assert math.isclose(p.open_pnl, 0.0, abs_tol=1e-9)


def test_before_match_values_match_the_spec():
    positions = _by_title("before_match")
    assert positions["Morocco wins"].stake == 5.0
    assert positions["Morocco wins"].size == 10.0
    assert positions["0-0 first half"].stake == 2.0
    assert positions["France 2-1"].stake == 5.0
    assert positions["France 2-1"].size == 25.0


def test_after_goal_moves_values_as_the_spec_describes():
    positions = _by_title("after_goal")
    assert math.isclose(positions["Morocco wins"].current_value, 10.0, rel_tol=1e-9)
    assert math.isclose(positions["0-0 first half"].current_value, 0.0, abs_tol=1e-9)
    assert math.isclose(positions["France 2-1"].current_value, 3.0, rel_tol=1e-9)


def test_after_cashout_drops_morocco_entirely():
    # A fully cashed-out position disappears from /positions. It does not
    # linger with size 0.
    positions = _by_title("after_cashout")
    assert "Morocco wins" not in positions
    assert len(positions) == 2


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_fixture_obeys_size_times_price_equals_value(scenario):
    for p in FixtureSource(scenario).fetch(WALLET):
        assert math.isclose(p.size * p.current_price, p.current_value, abs_tol=1e-6)
        assert math.isclose(p.size * p.entry_price, p.stake, abs_tol=1e-6)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_fixture_obeys_cash_pnl_equals_current_minus_initial(scenario):
    # Guard test. If Polymarket ever changes these semantics, this fails loudly
    # instead of the dashboard quietly lying.
    for p in FixtureSource(scenario).fetch(WALLET):
        assert math.isclose(p.open_pnl, p.current_value - p.stake, abs_tol=1e-9)


def test_fetch_ignores_the_wallet_argument():
    assert FixtureSource("before_match").fetch("anything") == FixtureSource(
        "before_match"
    ).fetch(WALLET)
