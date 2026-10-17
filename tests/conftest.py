"""Make predefined control scenarios independent of the operator's config."""
import pytest
import config
from controller_system.temperature_control import Thresholds
from controller_system.sensor_monitor import TemperatureSensorMonitor


@pytest.fixture(autouse=True)
def control_test_baseline(monkeypatch):
    # Existing control/PWM tests specify a 30% scenario. Production config may
    # deliberately use 10% (or another value); do not edit it to satisfy tests.
    # Tests for other limits explicitly override this temporary fixture value.
    monkeypatch.setattr(config, 'PELTIER_MAX_POWER', 30.0)
    monkeypatch.setattr(config, 'DEFAULT_LOW_C', 35.0)
    monkeypatch.setattr(config, 'DEFAULT_HIGH_C', 37.0)
    monkeypatch.setattr(config, 'TEMP_STALE_AFTER_S', 10.0)
    # These constructor defaults are bound when modules are imported, before
    # fixtures run. Freeze the scenarios here without changing config.py.
    monkeypatch.setattr(Thresholds.__init__, '__defaults__', (35.0, 37.0))
    defaults = TemperatureSensorMonitor.__init__.__defaults__
    monkeypatch.setattr(TemperatureSensorMonitor.__init__, '__defaults__', (10.0, *defaults[1:]))
