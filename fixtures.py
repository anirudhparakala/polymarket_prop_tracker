"""Offline PositionSource. Lets the dashboard be exercised with no network."""

from __future__ import annotations

import json
from pathlib import Path

from models import Position

SCENARIOS: tuple[str, ...] = ("before_match", "after_goal", "after_cashout")

DEFAULT_FIXTURE_DIR = Path(__file__).parent / "tests" / "fixtures"


class FixtureSource:
    """Satisfies models.PositionSource. The wallet argument is ignored."""

    def __init__(self, scenario: str, fixture_dir: Path | None = None):
        if scenario not in SCENARIOS:
            raise ValueError(
                f"Unknown scenario {scenario!r}. Expected one of {SCENARIOS}."
            )
        self._scenario = scenario
        self._dir = fixture_dir or DEFAULT_FIXTURE_DIR

    def fetch(self, wallet: str) -> list[Position]:
        path = self._dir / f"{self._scenario}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Position.from_api(row) for row in raw]
