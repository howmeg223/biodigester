"""
test_end_to_end.py - the whole system, over real MQTT.

Run with:   python3 -m pytest -v tests/test_end_to_end.py

These tests start their OWN simulator and controller as separate programs and
talk to them only through the broker, exactly as the app does. They need:
  * a broker running at the address in config.py  (otherwise they are skipped)
  * your own simulator and controller to be STOPPED (otherwise they are skipped)

Where test_control_logic.py proves the rules are right, this file proves the
parts are wired together correctly: the right topics, the right payloads, and
the safety behaviour that only exists between programs (the last-will message
and the heater's own command timeout).
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

import config
from mqtt_common import make_client

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RETAINED_TOPICS = [config.TOPIC_THRESHOLDS, config.TOPIC_STATUS,
                   config.TOPIC_HEATER_STATE, config.TOPIC_SIM_STATE]


class Listener:
    """A test client that remembers the latest message on every topic."""

    def __init__(self):
        self.latest = {}
        self.lock = threading.Lock()
        self.client = make_client(f"biodigester-test-{os.getpid()}")
        self.client.on_connect = lambda c, *a: c.subscribe(f"{config.ROOT}/#", qos=1)
        self.client.on_message = self._on_message
        self.client.connect(config.BROKER_HOST, config.BROKER_PORT)
        self.client.loop_start()

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except ValueError:
            return
        with self.lock:
            self.latest[msg.topic] = data

    def get(self, topic):
        with self.lock:
            return self.latest.get(topic) or {}

    def publish(self, topic, payload, retain=False):
        self.client.publish(topic, json.dumps(payload), qos=1, retain=retain)

    def clear_retained(self):
        for topic in RETAINED_TOPICS:
            self.client.publish(topic, b"", qos=1, retain=True)

    def wait_for(self, check, timeout, what):
        """Wait until check() is true, or fail the test saying what never happened."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.1)
        pytest.fail(f"Timed out after {timeout:.0f} s waiting for: {what}\n"
                    f"last status: {self.get(config.TOPIC_STATUS)}\n"
                    f"last heater: {self.get(config.TOPIC_HEATER_STATE)}")

    # Small readable helpers used by the tests below.
    def status(self):
        return self.get(config.TOPIC_STATUS)

    def heater(self):
        return self.get(config.TOPIC_HEATER_STATE).get("state")

    def set_fault(self, fault):
        self.publish(config.TOPIC_SIM_FAULT, {"fault": fault})

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


def start(script, *args):
    return subprocess.Popen([sys.executable, "-u", os.path.join(HERE, script), *args],
                            cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


@pytest.fixture(scope="module")
def system():
    """Start a simulator (cold, so the heater is needed) and a controller."""
    try:
        socket.create_connection((config.BROKER_HOST, config.BROKER_PORT), timeout=2).close()
    except OSError:
        pytest.skip(f"No MQTT broker at {config.BROKER_HOST}:{config.BROKER_PORT}. "
                    "Start Mosquitto to run the end-to-end tests.")

    listener = Listener()
    time.sleep(2.5)
    if listener.get(config.TOPIC_TEMPERATURE):
        listener.close()
        pytest.skip("A simulator is already publishing. Stop your own simulator and "
                    "controller before running the end-to-end tests.")
    listener.clear_retained()
    time.sleep(0.5)

    simulator = start("simulator.py", "--start", "28")
    running = {"controller": start("controller.py")}    # a test may replace this
    try:
        yield listener, running
    finally:
        for process in (running["controller"], simulator):
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        listener.clear_retained()
        time.sleep(0.5)
        listener.close()


def test_a_cold_digester_gets_heated(system):
    listener, _ = system
    listener.wait_for(lambda: listener.status().get("mode") == "HEATING", 15,
                      "the controller to report HEATING")
    listener.wait_for(lambda: listener.heater() == "ON", 5, "the heater to report ON")
    assert listener.status()["temperature"] < config.DEFAULT_LOW_C


def test_sensor_dropout_switches_the_heater_off(system):
    listener, _ = system
    listener.set_fault("sensor_dropout")
    listener.wait_for(lambda: listener.status().get("fault") == "SENSOR_STALE",
                      config.STALE_AFTER_S + 5, "a SENSOR_STALE fault")
    listener.wait_for(lambda: listener.heater() == "OFF", 5, "the heater to report OFF")
    assert listener.status()["mode"] == "FAULT"
    assert listener.status()["heating"] is False

    listener.set_fault("none")
    listener.wait_for(lambda: listener.status().get("mode") == "HEATING",
                      config.MIN_OFF_S + 10, "heating to resume once readings return")
    listener.wait_for(lambda: listener.heater() == "ON", 5, "the heater to report ON")


def test_nonsense_reading_switches_the_heater_off(system):
    listener, _ = system
    listener.set_fault("sensor_bad")
    listener.wait_for(lambda: listener.status().get("fault") == "SENSOR_BAD_VALUE", 6,
                      "a SENSOR_BAD_VALUE fault")
    listener.wait_for(lambda: listener.heater() == "OFF", 5, "the heater to report OFF")

    listener.set_fault("none")
    listener.wait_for(lambda: listener.status().get("mode") == "HEATING",
                      config.MIN_OFF_S + 10, "heating to resume once readings are sane")


def test_broken_heater_raises_an_alarm(system):
    listener, _ = system
    listener.wait_for(lambda: listener.heater() == "ON", 10, "the heater to be ON first")
    listener.set_fault("heater_failure")
    listener.wait_for(lambda: "HEATER_NOT_RESPONDING" in (listener.status().get("alarms") or []),
                      config.ACTUATOR_TIMEOUT_S + 6, "a HEATER_NOT_RESPONDING alarm")

    listener.set_fault("none")
    listener.wait_for(lambda: "HEATER_NOT_RESPONDING" not in (listener.status().get("alarms") or []),
                      10, "the alarm to clear once the heater works again")


def test_bad_thresholds_are_rejected_and_good_ones_applied(system):
    listener, _ = system
    listener.publish(config.TOPIC_THRESHOLDS, {"low": 40, "high": 38}, retain=True)
    listener.wait_for(lambda: "rejected" in listener.status().get("note", ""), 5,
                      "the controller to reject upside-down thresholds")
    assert listener.status()["low"] == config.DEFAULT_LOW_C
    assert listener.status()["high"] == config.DEFAULT_HIGH_C

    listener.publish(config.TOPIC_THRESHOLDS, {"low": 33.5, "high": 36.5}, retain=True)
    listener.wait_for(lambda: listener.status().get("low") == 33.5, 5,
                      "the controller to apply sane thresholds")
    assert listener.status()["high"] == 36.5

    listener.publish(config.TOPIC_THRESHOLDS,
                     {"low": config.DEFAULT_LOW_C, "high": config.DEFAULT_HIGH_C}, retain=True)
    listener.wait_for(lambda: listener.status().get("low") == config.DEFAULT_LOW_C, 5,
                      "the default thresholds to be restored")


def test_stopping_the_controller_switches_the_heater_off(system):
    """A normal stop (Ctrl+C) must leave the heater off straight away, well
    before the heater's own command timeout would have done it."""
    listener, running = system
    listener.wait_for(lambda: listener.heater() == "ON", config.MIN_OFF_S + 10,
                      "the heater to be ON before the stop")
    running["controller"].send_signal(signal.SIGINT)
    running["controller"].wait(timeout=8)

    listener.wait_for(lambda: listener.heater() == "OFF", 4,
                      "the heater to be switched off by the controller's last command")
    assert listener.status()["mode"] == "OFFLINE"

    # Bring a controller back for the next test.
    running["controller"] = start("controller.py")
    listener.wait_for(lambda: listener.status().get("mode") == "HEATING",
                      config.MIN_OFF_S + 15, "a restarted controller to resume heating")


def test_a_crashed_controller_cannot_leave_the_heater_on(system):
    """Kill the controller without warning while it is heating.

    Two things must happen with no help from the controller:
      1. the broker announces it OFFLINE (the last-will message)
      2. the heater notices the silence and switches itself off
    """
    listener, running = system
    listener.wait_for(lambda: listener.heater() == "ON", config.MIN_OFF_S + 10,
                      "the heater to be ON before the crash")
    running["controller"].kill()

    listener.wait_for(lambda: listener.status().get("mode") == "OFFLINE", 5,
                      "the last-will OFFLINE status")
    listener.wait_for(lambda: listener.heater() == "OFF",
                      config.HEATER_COMMAND_TIMEOUT_S + 5,
                      "the heater to switch itself off")
