"""Make power-control scenarios independent of the operator's local config."""
import pytest
import config


@pytest.fixture(autouse=True)
def power_test_baseline(monkeypatch):
    # Existing control/PWM tests specify a 30% scenario. Production config may
    # deliberately use 10% (or another value); do not edit it to satisfy tests.
    # Tests for other limits explicitly override this temporary fixture value.
    monkeypatch.setattr(config, 'PELTIER_MAX_POWER', 30.0)
