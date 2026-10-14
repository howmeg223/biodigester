"""Checks for the current folder-based services, without a broker or hardware."""
import csv
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import config
from controller_system.biodigester_controller import BiodigesterController
from controller_system.fault_manager import FaultManager
from controller_system.sensor_monitor import TemperatureSensorMonitor
from controller_system.temperature_control import TemperatureController
from monitoring.monitoring_service import MonitoringService
from monitoring.notification_manager import NotificationManager
from simulation.simulator_service import SimulatorService


@pytest.fixture
def monitor(tmp_path):
    return MonitoringService(tmp_path / "readings.csv", tmp_path / "faults.csv")


def receive(monitor, topic, data, retained=False):
    monitor.handle_message(topic, json.dumps(data).encode(), retained)


def mqtt_client():
    client = Mock()
    client.is_connected.return_value = True
    client.publish.return_value = SimpleNamespace(rc=0)
    return client


def test_live_and_retained_readings_have_different_logging_rules(monitor):
    receive(monitor, config.TOPIC_TEMPERATURE, {"temperature": 35}, retained=True)
    assert monitor.latest["temperature"][2] is True
    assert not monitor.log_file.exists()

    receive(monitor, config.TOPIC_TEMPERATURE, {"temperature": 36})
    receive(monitor, config.TOPIC_PRESSURE, {"pressure": 2})
    with monitor.log_file.open() as log_file:
        rows = list(csv.DictReader(log_file))
    assert [(row["sensor"], float(row["value"])) for row in rows] == [
        ("temperature", 36), ("pressure", 2),
    ]


@pytest.mark.parametrize("payload", [
    b"broken", b"\xff", b"null", b"{}", b'{"temperature": true}',
    b'{"temperature": "bad"}', b'{"temperature": NaN}',
    b'{"temperature": Infinity}', b'{"temperature": [36]}',
])
def test_invalid_readings_are_not_saved(monitor, payload):
    monitor.handle_message(config.TOPIC_TEMPERATURE, payload)
    assert not monitor.latest
    assert not monitor.log_file.exists()


def test_notification_history_is_bounded_newest_first_and_copies_events(capsys):
    notifications = NotificationManager(max_events=2)
    timestamp = datetime(2026, 10, 13, 12, 0)
    original = {"event": "RAISED", "code": "TEST", "message": "Check heater"}
    notifications.process(original, timestamp)
    assert "Check heater" in capsys.readouterr().out
    assert "received_at" not in original
    notifications.process({"event": "ACKNOWLEDGED", "code": "TEST"}, timestamp)
    notifications.process({"event": "CLEARED", "code": "TEST"}, timestamp)
    assert capsys.readouterr().out == ""
    assert [event["event"] for event in notifications.recent()] == ["CLEARED", "ACKNOWLEDGED"]


def test_fault_events_are_saved_and_available_for_dashboard(monitor):
    event = {"event": "RAISED", "code": "TEMP_SENSOR_STALE", "severity": "CRITICAL"}
    receive(monitor, config.TOPIC_FAULT_EVENT, event)
    with monitor.fault_log_file.open() as log_file:
        rows = list(csv.DictReader(log_file))
    assert rows[0]["code"] == event["code"]
    assert monitor.notifications.recent()[0]["event"] == "RAISED"
    receive(monitor, config.TOPIC_FAULT_STATE, {"faults": [event]})
    assert monitor.active_faults() == [event]


def test_mqtt_callbacks_subscribe_receive_and_disconnect(monitor, monkeypatch):
    client = mqtt_client()
    monkeypatch.setattr("monitoring.monitoring_service.make_client", lambda name: client)
    monitor.start()
    client.on_connect(client, None, None, 0)
    expected_topics = set(monitor.sensor_topics) | {
        config.TOPIC_STATUS, config.TOPIC_PELTIER_STATE, config.TOPIC_FAULT_STATE,
        config.TOPIC_FAULT_EVENT, config.TOPIC_SIM_STATE, config.TOPIC_BUBBLE,
        config.TOPIC_BUBBLE_EVENT, config.TOPIC_PRESSURE_STATUS, config.TOPIC_PRESSURE_RAW,
        config.TOPIC_PRESSURE_DROP, config.TOPIC_PRESSURE_THRESHOLD,
    }
    assert {call.args[0] for call in client.subscribe.call_args_list} == expected_topics
    assert monitor.connected
    client.on_message(client, None, SimpleNamespace(
        topic=config.TOPIC_TEMPERATURE, payload=b'{"temperature": 36}', retain=False))
    assert monitor.latest["temperature"][0] == 36
    client.on_disconnect(client, None, None, 0)
    assert not monitor.connected
    monitor.stop()
    client.disconnect.assert_called_once()
    client.loop_stop.assert_called_once()


def test_only_fresh_live_status_allows_range_requests(monitor, monkeypatch):
    monitor.client = mqtt_client()
    monitor.connected = True
    receive(monitor, config.TOPIC_STATUS, {"mode": "IDLE"}, retained=True)
    assert "live controller" in monitor.set_range(35, 37)
    receive(monitor, config.TOPIC_STATUS, {"mode": "IDLE"})
    assert monitor.set_range(35, 37) == "Range request sent."
    call = monitor.client.publish.call_args
    assert call.args[0] == config.TOPIC_THRESHOLDS
    assert json.loads(call.args[1]) == {"low": 35, "high": 37}
    assert call.kwargs["retain"] is True

    received_at = monitor.messages[config.TOPIC_STATUS][1]
    monkeypatch.setattr("monitoring.monitoring_service.time.monotonic",
                        lambda: received_at + config.TEMP_STALE_AFTER_S + 1)
    assert monitor.controller_status()["mode"] == "OFFLINE"


def test_requests_report_send_errors_and_commands_are_not_retained(monitor):
    monitor.client = mqtt_client()
    assert monitor.set_fault("heater_no_heat") == "Simulator scenario sent."
    assert monitor.client.publish.call_args.kwargs["retain"] is False
    assert monitor.acknowledge_faults() == "Acknowledgement/reset request sent."
    assert monitor.client.publish.call_args.kwargs["retain"] is False
    monitor.client.publish.return_value.rc = 1
    assert "Could not send" in monitor.acknowledge_faults()
    monitor.client.is_connected.return_value = False
    assert "Nothing was sent" in monitor.set_fault("none")


def test_latching_fault_requires_clear_condition_and_acknowledgement():
    faults = FaultManager()
    code = "HEATER_STUCK_ON"
    faults.update_condition(code, True, 0)
    assert faults.safety_trip_required()
    faults.update_condition(code, False, 1)
    assert code in faults.active_codes()
    assert faults.acknowledge(code, 2) == [code]
    assert not faults.heating_inhibited()
    assert [event["event"] for event in faults.pop_events()] == [
        "RAISED", "CONDITION_CLEARED", "ACKNOWLEDGED", "CLEARED",
    ]
    assert faults.pop_events() == []


def test_sensor_check_rejects_stale_readings():
    sensor = TemperatureSensorMonitor(stale_after_s=10)
    assert sensor.evaluate(0).value is None
    sensor.add_reading(36, 0)
    assert sensor.evaluate(1).trustworthy
    assert sensor.evaluate(11).conditions["TEMP_SENSOR_STALE"]
    assert sensor.evaluate(11).value is None


def test_temperature_rule_holds_in_band_but_safety_overrides_timer():
    controller = TemperatureController()
    assert controller.update(0, 34).heating
    assert controller.update(1, 35).heating
    assert controller.update(2, 37).heating
    assert not controller.update(3, 36, heating_allowed=False).heating


def test_controller_heats_then_stops_on_sensor_failure():
    controller = BiodigesterController(mqtt_client(), clock=lambda: 0)
    controller.feedback_received_at = 0
    controller.heater_relay_on = False
    controller.temperature_sensor.add_reading(34, 0)
    assert controller.step()["mode"] == "HEATING"
    controller.temperature_sensor.add_reading(float("nan"), 0)
    status = controller.step()
    assert status["mode"] == "FAULT"
    assert not status["heating"]
    assert "TEMP_SENSOR_OUT_OF_RANGE" in controller.faults.active_codes()


def test_simulator_freezes_sensor_at_current_model_temperature():
    simulator = SimulatorService(mqtt_client(), start_c=34)
    simulator.model.temp_c = 36
    simulator.fault_injector.apply("temp_sensor_frozen")
    simulator.model.temp_c = 38
    assert simulator.temperature_sensor.read(simulator.model.temp_c) == 36


def test_dashboard_renders_current_service_data(monitor, monkeypatch):
    import dashboard

    monkeypatch.setattr(dashboard, "monitor", monitor)
    monkeypatch.setattr(dashboard, "lock", monitor.lock)
    monitor.connected = True
    receive(monitor, config.TOPIC_TEMPERATURE, {"temperature": 36})
    receive(monitor, config.TOPIC_STATUS, {"mode": "IDLE", "low": 35, "high": 37})
    values, stamps, connection = dashboard.refresh_cards(0)
    assert values[0] == "36.00 °C"
    assert "updated" in stamps[0]
    assert "Connected" in connection
    children, disabled, sim_text, faults, events = dashboard.refresh_controller(0)
    assert disabled is False
    assert faults[0].children == "No active faults or alarms."
    assert events[0].children == "No fault events received yet."
    event = {"event": "RAISED", "code": "TEMP_SENSOR_STALE", "severity": "CRITICAL"}
    receive(monitor, config.TOPIC_FAULT_EVENT, event)
    receive(monitor, config.TOPIC_FAULT_STATE, {"faults": [event]})
    children, disabled, sim_text, faults, events = dashboard.refresh_controller(1)
    assert "TEMP_SENSOR_STALE" in faults[0].children[0].children
    assert "RAISED TEMP_SENSOR_STALE (CRITICAL)" in events[0].children
    assert list(dashboard.history_fig("temperature", None).data[0].y) == [36]
    http = dashboard.app.server.test_client()
    for path in ("/", "/_dash-layout", "/_dash-dependencies"):
        assert http.get(path).status_code == 200
