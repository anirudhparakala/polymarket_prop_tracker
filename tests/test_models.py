import math

from models import CheckpointRow, Position, Status

# Polymarket's own documented example row, used to pin field semantics.
RAW = {
    "asset": "71321045679252212594626385532706912750332728571942532289631379312455583992563",
    "conditionId": "0xd007d71fd17b0913b9d7ff198f617caa96a9e4aab1bed7d6f9abd76bb17dd507",
    "title": "Will Morocco win?",
    "eventSlug": "morocco-france",
    "outcome": "Yes",
    "size": 90548.087076,
    "avgPrice": 0.020628,
    "initialValue": 1867.825940203728,
    "currentValue": 5840.351616402,
    "cashPnl": 3972.525676198273,
    "percentPnl": 212.6817917393834,
    "totalBought": 109548.077076,
    "realizedPnl": -894.398503,
    "curPrice": 0.0645,
    "redeemable": False,
    "endDate": "2024-11-05",
}


def test_from_api_maps_stake_to_initial_value_not_total_bought():
    p = Position.from_api(RAW)
    assert p.stake == RAW["initialValue"]
    # totalBought is a SHARE COUNT. Using it as stake yields -103707 open_pnl.
    assert p.stake != RAW["totalBought"]


def test_from_api_open_pnl_equals_cash_pnl():
    p = Position.from_api(RAW)
    assert math.isclose(p.open_pnl, RAW["cashPnl"], rel_tol=1e-9)
    assert math.isclose(p.open_pnl, p.current_value - p.stake, rel_tol=1e-9)


def test_from_api_renames_fields_to_internal_shape():
    p = Position.from_api(RAW)
    assert p.market_title == "Will Morocco win?"
    assert p.entry_price == RAW["avgPrice"]
    assert p.current_price == RAW["curPrice"]
    assert p.event_slug == "morocco-france"


def test_from_api_tolerates_missing_fields():
    p = Position.from_api({"asset": "abc"})
    assert p.asset == "abc"
    assert p.size == 0.0
    assert p.market_title == ""
    assert p.redeemable is False


def test_position_is_frozen():
    p = Position.from_api(RAW)
    try:
        p.size = 1.0
    except Exception:
        return
    raise AssertionError("Position must be immutable")


def test_checkpoint_row_from_position_round_trips_join_key():
    p = Position.from_api(RAW)
    c = CheckpointRow.from_position(p)
    assert c.asset == p.asset
    assert c.current_value == p.current_value
    assert c.size == p.size


def test_status_values_are_the_five_documented_labels():
    assert {s.value for s in Status} == {
        "Open",
        "Reduced",
        "Increased",
        "Closed",
        "New",
    }
