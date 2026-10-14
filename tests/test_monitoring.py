"""Integration contracts between the imported dashboard and existing controller."""
import csv
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import config
from monitoring.monitoring_service import MonitoringService


@pytest.fixture
def monitor(tmp_path):
    return MonitoringService(tmp_path / "data" / "readings.csv")


def receive(monitor, topic, data, retained=False):
    monitor.handle_message(topic, json.dumps(data).encode(), retained)


def test_project_sensor_payloads_and_archive_numeric_format(monitor):
    receive(monitor, config.TOPIC_TEMPERATURE, {"temperature": 36.42})
    receive(monitor, config.SENSORS["pressure"]["topic"], {"pressure": 2.43})
    receive(monitor, config.SENSORS["ph"]["topic"], {"ph": 7.02})
    with monitor.log_file.open() as f:
        rows = list(csv.DictReader(f))
    assert [(r["sensor"], float(r["value"])) for r in rows] == [
        ("temperature", 36.42), ("pressure", 2.43), ("ph", 7.02)]


def test_retained_sensor_is_displayed_but_not_logged(monitor):
    receive(monitor, config.TOPIC_TEMPERATURE, {"temperature": 36}, True)
    assert monitor.latest["temperature"][2] is True
    assert not monitor.log_file.exists()


@pytest.mark.parametrize("payload", [b"garbled", b"\xff", b"{}", b"null", b"true",
                                    b"NaN", b"Infinity", b'{"temperature": "bad"}',
                                    b'{"temperature": [36]}'])
def test_malformed_sensor_never_becomes_a_live_reading(monitor, payload):
    monitor.handle_message(config.TOPIC_TEMPERATURE, payload)
    assert not monitor.latest
    assert not monitor.log_file.exists()


def test_retained_and_stale_controller_cannot_look_live(monitor, monkeypatch):
    monitor.connected = True
    receive(monitor, config.TOPIC_STATUS, {"mode": "HEATING", "heating": True}, True)
    assert monitor.controller_status()["mode"] == "OFFLINE"
    receive(monitor, config.TOPIC_STATUS, {"mode": "HEATING", "heating": True})
    assert monitor.controller_status()["mode"] == "HEATING"
    received = monitor.messages[config.TOPIC_STATUS][1]
    monkeypatch.setattr("dashboard.time.monotonic", lambda: received + config.TEMP_STALE_AFTER_S + 1)
    assert monitor.controller_status()["mode"] == "OFFLINE"


@pytest.mark.parametrize("data", [{}, {"mode": "UNKNOWN"}, {"mode": "IDLE", "low": "bad"},
                                 {"mode": "IDLE", "alarm_text": None}])
def test_bad_status_does_not_replace_controller_state(monitor, data):
    monitor.connected = True
    receive(monitor, config.TOPIC_STATUS, {"mode": "IDLE", "reason": "Valid status"})
    receive(monitor, config.TOPIC_STATUS, data)
    assert monitor.controller_status()["reason"] == "Valid status"


class Client:
    def __init__(self):
        self.sent = []
        self.connected = True

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, qos, retain):
        self.sent.append((topic, json.loads(payload), qos, retain))
        return SimpleNamespace(rc=0)


def test_range_validation_and_controller_protocol(monitor):
    monitor.client = Client()
    monitor.connected = True
    receive(monitor, config.TOPIC_STATUS, {"mode": "IDLE", "heating": False})
    for low, high in [(40, 38), (35, 35.1), (0, 37), (35, 100), (None, 37), (35, float("nan"))]:
        assert "sent" not in monitor.set_range(low, high)
    assert not monitor.client.sent
    assert "sent" in monitor.set_range(35, 37)
    assert monitor.client.sent == [(config.TOPIC_THRESHOLDS, {"low": 35.0, "high": 37.0}, 1, True)]
    monitor.client.connected = False
    assert "Nothing was sent" in monitor.set_range(35, 37)
    assert len(monitor.client.sent) == 1


def test_offline_range_and_disabled_faults_do_not_publish(monitor, monkeypatch):
    monitor.client = Client()
    assert "live controller" in monitor.set_range(35, 37)
    monkeypatch.setattr(config, "SHOW_SIM_PANEL", False)
    assert "disabled" in monitor.set_fault("temp_sensor_dropout")
    assert not monitor.client.sent


def test_fault_commands_are_not_retained(monitor):
    monitor.client = Client()
    assert "sent" in monitor.set_fault("temp_sensor_dropout")
    assert monitor.client.sent == [(config.TOPIC_SIM_FAULT, {"fault": "temp_sensor_dropout"}, 1, False)]


def test_browser_layout_callbacks_and_report_use_project_messages(monitor, monkeypatch):
    import dashboard
    monkeypatch.setattr(dashboard, "monitor", monitor)
    monkeypatch.setattr(dashboard, "lock", monitor.lock)
    http = dashboard.app.server.test_client()
    for url in ("/", "/_dash-layout", "/_dash-dependencies"):
        assert http.get(url).status_code == 200
    monitor.connected = True
    receive(monitor, config.TOPIC_TEMPERATURE, {"temperature": 36.42})
    receive(monitor, config.TOPIC_STATUS, {
        "mode": "HEATING", "heating": True, "low": 35.0, "high": 37.0,
        "reason": "Heating", "alarm_text": ["Temperature is well below the target range."],
        "note": "Range set to 35 to 37 °C."})
    values, stamps, connection = dashboard.refresh_cards(0)
    assert values[0] == "36.42 °C"
    assert "updated" in stamps[0]
    assert "Connected" in connection
    children, disabled, _, _, _ = dashboard.refresh_controller(0)
    assert disabled is False
    assert any(getattr(child, "children", "") == "Range set to 35 to 37 °C." for child in children)
    history = dashboard.history_fig("temperature", None)
    assert list(history.data[0].y) == [36.42]
    assert history.layout.shapes[0].y0 == 35
    assert len(dashboard.report_fig(["temperature", "pressure", "ph"], None).data) == 3
    assert dashboard.report_fig([], None).layout.annotations[0].text == "Tick at least one sensor"
    # Exercise the HTTP callback path used by the actual browser refresh.
    callback_key = next(k for k in dashboard.app.callback_map if '"val"' in k)
    response = http.post("/_dash-update-component", json={
        "output": callback_key,
        "outputs": [
            [{"id": {"type": "val", "key": k}, "property": "children"} for k in config.SENSORS],
            [{"id": {"type": "ts", "key": k}, "property": "children"} for k in config.SENSORS],
            {"id": "status", "property": "children"}],
        "inputs": [{"id": "tick", "property": "n_intervals", "value": 1}],
        "state": [], "changedPropIds": ["tick.n_intervals"]})
    assert response.status_code == 200
    assert "36.42" in response.get_data(as_text=True)


@pytest.mark.parametrize("demo", [False, True])
@pytest.mark.parametrize("server_fails", [False, True])
def test_dashboard_demo_owns_and_stops_only_its_children(monkeypatch, demo, server_fails):
    import dashboard
    children = [Mock(), Mock()]
    for child in children:
        child.poll.return_value = None
    spawn = Mock(side_effect=children)
    start = Mock()
    stop = Mock()
    run = Mock(side_effect=RuntimeError("Server could not start") if server_fails else None)
    monkeypatch.setattr(dashboard.subprocess, "Popen", spawn)
    monkeypatch.setattr(dashboard.monitor, "start", start)
    monkeypatch.setattr(dashboard.monitor, "stop", stop)
    monkeypatch.setattr(dashboard.app, "run", run)
    monkeypatch.setattr(dashboard.sys, "argv", ["dashboard.py"] + (["--demo"] if demo else []))
    if server_fails:
        with pytest.raises(RuntimeError, match="Server could not start"):
            dashboard.main()
    else:
        dashboard.main()
    start.assert_called_once()
    stop.assert_called_once()
    assert spawn.call_count == (2 if demo else 0)
    if demo:
        assert [call.args[0][-1].split("/")[-1] for call in spawn.call_args_list] == ["controller.py", "simulator.py"]
        for child in children:
            child.send_signal.assert_called_once_with(dashboard.signal.SIGINT)
            child.wait.assert_called_once_with(timeout=5)


def test_demo_cleans_up_if_second_child_cannot_start(monkeypatch):
    import dashboard
    child = Mock()
    child.poll.return_value = None
    stop = Mock()
    monkeypatch.setattr(dashboard.subprocess, "Popen", Mock(side_effect=[child, OSError("Cannot start simulator")]))
    monkeypatch.setattr(dashboard.monitor, "stop", stop)
    monkeypatch.setattr(dashboard.sys, "argv", ["dashboard.py", "--demo"])
    with pytest.raises(OSError, match="Cannot start simulator"):
        dashboard.main()
    child.send_signal.assert_called_once_with(dashboard.signal.SIGINT)
    child.wait.assert_called_once_with(timeout=5)
    stop.assert_called_once()
