"""
test_control_logic.py - tests for the regulation logic.

Run with:   python3 -m pytest -v

None of these tests need MQTT, a broker or any hardware. They are grouped by
the testing idea behind them:

  A. Boundary value tests of the on/off rule
  B. Fault conditions: readings that must not be trusted
  C. Threshold validation
  D. Regulator behaviour: normal cycle, timers, alarms, heater feedback
  E. A metamorphic test (no expected values needed, only a relation)
  F. Closed-loop tests against the simulated digester
"""

import random

import pytest

from control_logic import (
    Regulator, Settings, Thresholds,
    check_thresholds, decide_hysteresis, decide_simple,
)
from fault_handling import (
    FaultHandler, HEATER_NOT_RESPONDING, NO_READING, SENSOR_BAD_VALUE,
    SENSOR_STALE, TEMP_HIGH, TEMP_LOW,
)
from simulation.digester_model import DigesterModel


def test_simulator_publishes_all_demo_sensors_and_peltier_feedback(monkeypatch):
    import json
    from unittest.mock import Mock
    from types import SimpleNamespace
    import config
    from simulation.simulator_service import SimulatorService
    client = Mock()
    client.is_connected.return_value = True
    sim = SimulatorService(client, start_c=28)
    monkeypatch.setattr("simulation.devices.random.gauss", lambda *_: 0)
    sim._on_message(client, None, SimpleNamespace(topic=config.TOPIC_PELTIER_CMD,
                    payload=b'{"power":100}', retain=False))
    sim.publish_readings(14)
    sim.publish_ph_if_due(0)
    messages = {c.args[0]: json.loads(c.args[1]) for c in client.publish.call_args_list}
    assert messages[config.TOPIC_TEMPERATURE] == {"temperature": 28.0}
    assert messages[config.TOPIC_PRESSURE] == {"pressure": 2.0}
    assert messages[config.TOPIC_PH]["simulated"] is True
    assert messages[config.TOPIC_PELTIER_STATE]["applied_power"] == 30

BAND = Thresholds(low=35.0, high=37.0)
ON, OFF = True, False


def make_regulator(**overrides):
    """A Regulator with no minimum on/off time unless a test asks for one."""
    values = dict(min_on_s=0.0, min_off_s=0.0, stale_after_s=10.0)
    values.update(overrides)
    return Regulator(Thresholds(35.0, 37.0), Settings(**values))


# ---------------------------------------------------------------------------
# A. Boundary value tests of the on/off rule
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("temp, heating_now, expected", [
    # just below the low threshold: always heat
    (34.99, OFF, ON),
    (34.99, ON, ON),
    # exactly on the low threshold: inside the band, so hold
    (35.00, OFF, OFF),
    (35.00, ON, ON),
    # just above the low threshold: hold
    (35.01, OFF, OFF),
    (35.01, ON, ON),
    # middle of the band: hold
    (36.00, OFF, OFF),
    (36.00, ON, ON),
    # just below the high threshold: hold
    (36.99, OFF, OFF),
    (36.99, ON, ON),
    # exactly on the high threshold: inside the band, so hold
    (37.00, OFF, OFF),
    (37.00, ON, ON),
    # just above the high threshold: always stop
    (37.01, OFF, OFF),
    (37.01, ON, OFF),
])
def test_hysteresis_boundaries(temp, heating_now, expected):
    assert decide_hysteresis(temp, heating_now, BAND) is expected


@pytest.mark.parametrize("temp, expected", [
    (35.99, ON),
    (36.00, OFF),    # exactly on the setpoint is not "below" it
    (36.01, OFF),
])
def test_simple_boundaries(temp, expected):
    assert decide_simple(temp, setpoint=36.0) is expected


# ---------------------------------------------------------------------------
# B. Fault conditions: readings that must not be trusted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("temp, age, expected", [
    (36.0, 1.0, None),                      # a normal, fresh reading
    (None, None, NO_READING),               # nothing has ever arrived
    (36.0, 10.0, None),                     # exactly at the stale limit: still ok
    (36.0, 10.1, SENSOR_STALE),             # just past the stale limit
    (-127.0, 1.0, SENSOR_BAD_VALUE),        # a disconnected-probe style value
    (-0.1, 1.0, SENSOR_BAD_VALUE),          # just below the plausible range
    (0.0, 1.0, None),                       # bottom edge of the plausible range
    (70.0, 1.0, None),                      # top edge of the plausible range
    (70.1, 1.0, SENSOR_BAD_VALUE),          # just above the plausible range
    (float("nan"), 1.0, SENSOR_BAD_VALUE),  # not a number at all
    (float("inf"), 1.0, SENSOR_BAD_VALUE),
])
def test_check_reading(temp, age, expected):
    assert FaultHandler(Settings(stale_after_s=10.0)).check_reading(temp, age) == expected


def test_stale_reading_forces_heater_off():
    reg = make_regulator()
    assert reg.update(now=0, temp_c=30.0, reading_age_s=0).heating is ON
    decision = reg.update(now=20, temp_c=30.0, reading_age_s=20)
    assert decision.heating is OFF
    assert decision.mode == "FAULT"
    assert decision.fault == SENSOR_STALE


def test_bad_value_forces_heater_off():
    reg = make_regulator()
    assert reg.update(0, 30.0, 0).heating is ON
    decision = reg.update(1, -127.0, 0)
    assert decision.heating is OFF
    assert decision.fault == SENSOR_BAD_VALUE


def test_no_reading_means_no_heating():
    decision = make_regulator().update(0, None, None)
    assert decision.heating is OFF
    assert decision.fault == NO_READING


def test_fault_beats_the_minimum_on_time():
    """A fault must switch the heater off even if it only just came on."""
    reg = make_regulator(min_on_s=300.0)
    assert reg.update(0, 30.0, 0).heating is ON
    assert reg.update(1, float("nan"), 0).heating is OFF


def test_heating_resumes_after_the_fault_clears():
    reg = make_regulator()
    reg.update(0, 30.0, 0)
    assert reg.update(20, 30.0, 20).mode == "FAULT"
    decision = reg.update(21, 30.0, 0)       # a fresh reading arrives
    assert decision.fault is None
    assert decision.heating is ON


# ---------------------------------------------------------------------------
# C. Threshold validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("low, high, accepted", [
    (35.0, 37.0, True),
    (20.0, 60.0, True),      # the widest band allowed
    (36.0, 36.5, True),      # exactly the minimum width
    (36.0, 36.4, False),     # narrower than the minimum width
    (37.0, 35.0, False),     # the wrong way round
    (36.0, 36.0, False),     # no band at all
    (19.9, 37.0, False),     # low is below the allowed range
    (35.0, 60.1, False),     # high is above the allowed range
    ("abc", 37.0, False),    # not a number
    (None, 37.0, False),
    (float("nan"), 37.0, False),
])
def test_check_thresholds(low, high, accepted):
    problem = check_thresholds(low, high, Settings())
    assert (problem is None) is accepted


def test_rejected_thresholds_leave_the_old_ones_in_place():
    reg = make_regulator()
    assert reg.set_thresholds(40.0, 38.0) is not None
    assert (reg.th.low, reg.th.high) == (35.0, 37.0)


def test_accepted_thresholds_change_the_behaviour():
    reg = make_regulator()
    assert reg.update(0, 36.0, 0).heating is OFF     # inside 35-37: hold off
    assert reg.set_thresholds(38.0, 40.0) is None
    assert reg.update(1, 36.0, 0).heating is ON      # now below 38: heat


# ---------------------------------------------------------------------------
# D. Regulator behaviour
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("temperature, reading_age", [
    (34.0, 11.0),        # stale reading
    (float("nan"), 0),  # broken reading
    (None, None),       # missing reading
])
def test_fault_recovery_inside_range_keeps_heater_off(temperature, reading_age):
    """A recovered sensor must not restore the ON state from before the fault."""
    regulator = make_regulator()
    assert regulator.update(0, 34.0, 0).heating is ON
    assert regulator.update(1, temperature, reading_age).heating is OFF
    recovered = regulator.update(2, 36.0, 0)
    assert recovered.fault is None
    assert recovered.heating is OFF
    assert recovered.mode == "IDLE"


def test_fault_recovery_obeys_minimum_off_time():
    """After a fault, a cold tank must still wait before the heater restarts."""
    regulator = make_regulator(min_off_s=60.0)
    assert regulator.update(0, 34.0, 0).heating is ON
    assert regulator.update(1, 34.0, 11.0).heating is OFF
    assert regulator.update(2, 34.0, 0).heating is OFF
    assert regulator.update(60, 34.0, 0).heating is OFF
    assert regulator.update(61, 34.0, 0).heating is ON


def test_normal_heating_cycle():
    reg = make_regulator()
    readings = [34.0, 35.5, 36.9, 37.1, 36.5, 35.5, 34.9, 36.0]
    expected = [ON,   ON,   ON,   OFF,  OFF,  OFF,  ON,   ON]
    actual = [reg.update(t, temp, 0).heating for t, temp in enumerate(readings)]
    assert actual == expected


def test_modes_follow_the_heater():
    reg = make_regulator()
    assert reg.update(0, 34.0, 0).mode == "HEATING"
    assert reg.update(1, 38.0, 0).mode == "IDLE"


def test_minimum_on_time_delays_switching_off():
    reg = make_regulator(min_on_s=60.0)
    assert reg.update(0, 34.0, 0).heating is ON
    assert reg.update(30, 37.5, 0).heating is ON      # too soon to switch off
    assert reg.update(59, 37.5, 0).heating is ON
    assert reg.update(60, 37.5, 0).heating is OFF     # the minimum has passed


def test_minimum_off_time_delays_switching_on():
    reg = make_regulator(min_off_s=60.0)
    reg.update(0, 34.0, 0)                            # on
    assert reg.update(10, 37.5, 0).heating is OFF     # off at t=10
    assert reg.update(40, 34.0, 0).heating is OFF     # too soon to heat again
    assert reg.update(70, 34.0, 0).heating is ON      # 60 s after switching off


def test_far_too_hot_beats_the_minimum_on_time():
    reg = make_regulator(min_on_s=300.0, alarm_margin_c=2.0)
    assert reg.update(0, 34.0, 0).heating is ON
    decision = reg.update(1, 39.5, 0)                 # above 37 + 2
    assert decision.heating is OFF
    assert TEMP_HIGH in decision.alarms


@pytest.mark.parametrize("temp, expected_alarms", [
    (32.9, [TEMP_LOW]),     # just beyond low - margin
    (33.0, []),             # exactly on the alarm line: no alarm
    (36.0, []),
    (39.0, []),             # exactly on the alarm line: no alarm
    (39.1, [TEMP_HIGH]),    # just beyond high + margin
])
def test_temperature_alarms(temp, expected_alarms):
    reg = make_regulator(alarm_margin_c=2.0)
    assert reg.update(0, temp, 0).alarms == expected_alarms


def test_heater_not_responding_alarm():
    reg = make_regulator(actuator_timeout_s=5.0)
    # Told to heat, but the heater keeps reporting that it is off.
    first = reg.update(0, 34.5, 0, heater_reported_on=False)
    assert first.heating is ON
    assert first.alarms == []
    early = reg.update(4, 34.5, 0, heater_reported_on=False)
    late = reg.update(6, 34.5, 0, heater_reported_on=False)
    assert HEATER_NOT_RESPONDING not in early.alarms
    assert HEATER_NOT_RESPONDING in late.alarms
    # The heater finally responds: the alarm clears.
    fixed = reg.update(7, 34.5, 0, heater_reported_on=True)
    assert HEATER_NOT_RESPONDING not in fixed.alarms


def test_no_heater_feedback_means_no_heater_alarm():
    reg = make_regulator(actuator_timeout_s=5.0)
    reg.update(0, 34.5, 0)
    assert reg.update(100, 34.5, 0).alarms == []


def test_fault_handlers_have_independent_heater_timers():
    first = FaultHandler(Settings(actuator_timeout_s=5.0))
    second = FaultHandler(Settings(actuator_timeout_s=5.0))
    assert first.heater_alarms(0, commanded_on=True, reported_on=False) == []
    assert second.heater_alarms(4, commanded_on=True, reported_on=False) == []
    assert first.heater_alarms(5, commanded_on=True, reported_on=False) == [HEATER_NOT_RESPONDING]
    assert second.heater_alarms(5, commanded_on=True, reported_on=False) == []
    assert second.heater_alarms(9, commanded_on=True, reported_on=False) == [HEATER_NOT_RESPONDING]


@pytest.mark.parametrize("reported_on", [False, None])
def test_heater_agreement_or_missing_feedback_resets_fault_timer(reported_on):
    handler = FaultHandler(Settings(actuator_timeout_s=5.0))
    assert handler.heater_alarms(0, commanded_on=True, reported_on=False) == []
    assert handler.heater_alarms(5, commanded_on=False, reported_on=reported_on) == []
    assert handler.heater_alarms(6, commanded_on=True, reported_on=False) == []
    assert handler.heater_alarms(10, commanded_on=True, reported_on=False) == []
    assert handler.heater_alarms(11, commanded_on=True, reported_on=False) == [HEATER_NOT_RESPONDING]


def test_simple_strategy_chatters_and_hysteresis_does_not():
    """The same noisy readings around 36.0 fed to both strategies."""
    rng = random.Random(1)
    readings = [36.0 + rng.gauss(0, 0.1) for _ in range(200)]

    def count_switches(strategy):
        reg = make_regulator(strategy=strategy)
        states = [reg.update(t, temp, 0).heating for t, temp in enumerate(readings)]
        return sum(1 for a, b in zip(states, states[1:]) if a != b)

    assert count_switches("simple") > 50
    assert count_switches("hysteresis") == 0


# ---------------------------------------------------------------------------
# E. Metamorphic test
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("shift", [-10.0, -2.5, 3.0, 12.0])
def test_shifting_everything_changes_nothing(shift):
    """Metamorphic relation: if every reading AND both thresholds move by the
    same amount, the sequence of on/off decisions must be identical.

    No expected outputs are written down here. The test only checks that two
    runs agree, which is the point of metamorphic testing.
    """
    rng = random.Random(7)
    readings = [36.0 + rng.uniform(-3.0, 3.0) for _ in range(300)]

    original = make_regulator(alarm_margin_c=100.0)
    shifted = make_regulator(alarm_margin_c=100.0)
    assert shifted.set_thresholds(35.0 + shift, 37.0 + shift) is None

    for t, temp in enumerate(readings):
        a = original.update(t, temp, 0).heating
        b = shifted.update(t, temp + shift, 0).heating
        assert a == b


# ---------------------------------------------------------------------------
# F. Closed-loop tests against the simulated digester
# ---------------------------------------------------------------------------
def run_closed_loop(hours, sensor_fails_between=None, ambient_c=14.0):
    """Regulator and DigesterModel wired together, 10 simulated seconds per step.

    Returns a list of (time_s, true_temperature, heater_on).
    """
    model = DigesterModel(temp_c=36.0, slurry_kg=20.0, heater_w=150.0,
                          ua_w_per_k=2.0, heater_lag_s=180.0)
    reg = Regulator(Thresholds(35.0, 37.0),
                    Settings(min_on_s=300.0, min_off_s=300.0, stale_after_s=60.0))
    step_s = 10.0
    last_reading, last_reading_time = None, None
    log = []
    for i in range(int(hours * 3600 / step_s)):
        now = i * step_s
        sensor_working = not (sensor_fails_between
                              and sensor_fails_between[0] <= now < sensor_fails_between[1])
        if sensor_working:
            last_reading, last_reading_time = model.temp_c, now
        age = None if last_reading_time is None else now - last_reading_time
        decision = reg.update(now, last_reading, age)
        model.step(step_s, model.heater_w if decision.heating else 0.0, ambient_c)
        log.append((now, model.temp_c, decision.heating))
    return log


def test_closed_loop_holds_the_band_for_a_day():
    log = run_closed_loop(hours=24)
    temps = [temp for _, temp, _ in log]
    # Allowed to run slightly past the band because of the heater lag.
    assert min(temps) > 34.5
    assert max(temps) < 37.5


def test_closed_loop_does_not_cycle_excessively():
    log = run_closed_loop(hours=24)
    states = [on for _, _, on in log]
    switches = sum(1 for a, b in zip(states, states[1:]) if a != b)
    assert 2 <= switches <= 60


def test_closed_loop_sensor_dropout_turns_the_heater_off():
    """Sensor dies for 3 hours. The heater must be off for all of that time
    once the stale limit (60 s) has passed, and the digester must cool."""
    start, end = 4 * 3600, 7 * 3600
    log = run_closed_loop(hours=10, sensor_fails_between=(start, end))
    during_fault = [(t, temp, on) for t, temp, on in log if start + 70 <= t < end]
    assert all(on is False for _, _, on in during_fault)
    assert during_fault[-1][1] < during_fault[0][1]     # it cooled down


def test_closed_loop_recovers_after_the_sensor_returns():
    start, end = 4 * 3600, 7 * 3600
    log = run_closed_loop(hours=14, sensor_fails_between=(start, end))
    last_two_hours = [temp for t, temp, _ in log if t >= 12 * 3600]
    assert min(last_two_hours) > 34.5
    assert max(last_two_hours) < 37.5
