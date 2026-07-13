"""Present so pytest adds the repo root to sys.path; modules live flat at root.

Also makes the suite HERMETIC with respect to real credentials.
"""

import pytest


@pytest.fixture(autouse=True)
def _never_use_real_credentials(monkeypatch):
    """No test may read, or depend on, the developer's real `.env`.

    `app.py` calls `load_dotenv()` at import, and Streamlit's AppTest re-execs
    that source on every run -- so without this, a developer with real Polymarket
    US credentials on disk would have them loaded into the test process, and the
    suite's behaviour (which account the sidebar defaults to) would silently
    depend on whether a `.env` happened to exist. Tests would pass on one machine
    and fail on another, and a real trading key would be pulled into a test run.

    Neutralise both: stub out load_dotenv, and clear the variables it would set.
    """
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for var in ("POLYMARKET_US_KEY_ID", "POLYMARKET_US_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
