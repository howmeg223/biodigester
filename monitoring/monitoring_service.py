"""Receive MQTT messages, save readings and send dashboard requests.

MQTT delivers messages by topic (an address such as biodigester/sensors/temperature).
The controller makes heating decisions; this service supplies the dashboard's data.
"""
from collections import deque
from datetime import datetime
import json
import math
import threading
import time
import uuid

import config
from controller_system.temperature_control import check_thresholds
from monitoring.data_logger import DataLogger
from monitoring.notification_manager import NotificationManager
from mqtt_common import connected_ok, make_client, read_power


class MonitoringService:
    """Keep the latest readings and controller messages for the dashboard."""

    def __init__(self, reading_file=config.LOG_FILE, fault_file=config.FAULT_LOG_FILE, bubble_file=config.BUBBLE_LOG_FILE, log_enabled=True):
        # MQTT receives messages in a background thread. The lock prevents it
        # changing shared data while a dashboard callback is reading that data.
        self.lock = threading.RLock()
        self.latest = {}
        self.ph_simulated = False
        self.log_enabled = log_enabled
        self.bubble_file = bubble_file
        self.bubble_events = deque(maxlen=10000)
        self.bubble_seen = set()
        self.bubble_seen_order = deque()
        self.last_bubble = None
        self.messages = {}
        self.connected = False
        self.client = None
        self.sensor_topics = {}
        for sensor_name, sensor_settings in config.SENSORS.items():
            self.sensor_topics[sensor_settings["topic"]] = sensor_name
        self.logger = DataLogger(reading_file, fault_file)
        self.notifications = NotificationManager()
        self.log_file = self.logger.reading_file
        self.fault_log_file = self.logger.fault_file

    def handle_message(self, topic, payload, retained=False):
        """Check an incoming message before using or saving it.

        A retained message is a saved message from the broker. Display saved
        readings, but do not log them again as if they were new measurements.
        """
        try:
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        # Monotonic time measures elapsed seconds; calendar time labels CSV rows.
        received_at = time.monotonic()
        timestamp = datetime.now()
        with self.lock:
            if topic in self.sensor_topics:
                key = self.sensor_topics[topic]
                raw = data.get(key) if isinstance(data, dict) else None
                if isinstance(raw, bool):
                    return
                try:
                    value = float(raw)
                except (ValueError, TypeError, OverflowError):
                    return
                if data.get("valid") is False or not math.isfinite(value) or (key == "ph" and not 0 <= value <= 14):
                    return
                # Each reading stores: value, calendar time, saved flag, timer time.
                self.latest[key] = (value, timestamp, retained, received_at)
                if key == "ph":
                    self.ph_simulated = data.get("simulated") is True
                if not retained and self.log_enabled:
                    self.logger.log_reading(timestamp, key, value)
                return

            if topic == config.TOPIC_FAULT_EVENT:
                if isinstance(data, dict) and isinstance(data.get("code"), str):
                    self.notifications.process(data, timestamp)
                    if self.log_enabled:
                        self.logger.log_fault_event(timestamp, data)
                return

            if topic == config.TOPIC_BUBBLE_EVENT:
                if retained or not isinstance(data, dict) or data.get("event") != "bubble":
                    return
                count, session = data.get("count"), data.get("session")
                if isinstance(count, bool) or not isinstance(count, int) or count < 1 or not isinstance(session, str):
                    return
                identity = (session, count)
                if identity in self.bubble_seen:
                    return
                self.bubble_seen.add(identity)
                self.bubble_seen_order.append(identity)
                if len(self.bubble_seen_order) > 10000:
                    self.bubble_seen.discard(self.bubble_seen_order.popleft())
                self.bubble_events.append(received_at)
                self.last_bubble = timestamp
                if self.log_enabled:
                    self.logger.log_bubble_event(self.bubble_file, timestamp, data)
            elif topic == config.TOPIC_BUBBLE:
                if not isinstance(data, dict) or data.get("bubble") not in (True, False, 0, 1):
                    return
                if not isinstance(data.get("count"), int) or isinstance(data.get("count"), bool) or data["count"] < 0:
                    return
            elif topic == config.TOPIC_PRESSURE_STATUS:
                if not isinstance(data, dict) or not isinstance(data.get("online"), bool):
                    return
            elif topic in (config.TOPIC_PRESSURE_RAW, config.TOPIC_PRESSURE_DROP, config.TOPIC_PRESSURE_THRESHOLD):
                key = {config.TOPIC_PRESSURE_RAW: "raw", config.TOPIC_PRESSURE_DROP: "drop",
                       config.TOPIC_PRESSURE_THRESHOLD: "threshold"}[topic]
                if not isinstance(data, dict) or isinstance(data.get(key), bool):
                    return
                try:
                    if not math.isfinite(float(data[key])):
                        return
                except (KeyError, TypeError, ValueError, OverflowError):
                    return
            elif topic == config.TOPIC_STATUS:
                if not isinstance(data, dict) or data.get("mode") not in ("HEATING", "IDLE", "FAULT", "OFFLINE"):
                    return
                if "faults" in data and (not isinstance(data["faults"], list) or
                        any(not isinstance(f, dict) for f in data["faults"])):
                    return
                for key in ("low", "high"):
                    if key in data and (isinstance(data[key], bool) or not isinstance(data[key], (int, float)) or not math.isfinite(data[key])):
                        return
                if "alarm_text" in data and not isinstance(data["alarm_text"], list):
                    return
            elif topic == config.TOPIC_PELTIER_STATE:
                if not isinstance(data, dict):
                    return
                if data.get("online") is not False:
                    try:
                        read_power(data, "applied_power")
                    except ValueError:
                        return
                    if not isinstance(data.get("safety_latched"), bool):
                        return
            elif topic == config.TOPIC_FAULT_STATE:
                if not isinstance(data, dict) or not isinstance(data.get("faults", []), list):
                    return
                if any(not isinstance(f, dict) for f in data.get("faults", [])):
                    return
            elif topic == config.TOPIC_SIM_STATE:
                if not isinstance(data, dict):
                    return
            else:
                return
            self.messages[topic] = (data, received_at, retained)

    def snapshot(self):
        """Copy shared dictionaries while holding the lock."""
        with self.lock:
            return dict(self.latest), dict(self.messages), self.connected

    def controller_status(self):
        """Show OFFLINE until a recent, live controller message is available."""
        _, messages, connected = self.snapshot()
        entry = messages.get(config.TOPIC_STATUS)
        if not entry:
            return {"mode": "OFFLINE", "reason": "Waiting for the controller."}
        data, received, retained = entry
        if not connected or time.monotonic() - received > config.TEMP_STALE_AFTER_S:
            return {"mode": "OFFLINE", "reason": "No live controller status. Check the controller and broker."}
        if retained and data.get("mode") != "OFFLINE":
            return {"mode": "OFFLINE", "reason": "Saved status received; waiting for a live controller update."}
        return dict(data)

    def active_faults(self):
        _, messages, _ = self.snapshot()
        entry = messages.get(config.TOPIC_FAULT_STATE)
        if not entry:
            return []
        data, received_at, retained = entry
        return list(data.get("faults", []))

    def pressure_summary(self):
        with self.lock:
            entry = self.messages.get(config.TOPIC_PRESSURE_STATUS)
            if not entry:
                return {"live": False, "description": "Waiting for pressure hardware"}
            data, received, retained = entry
            live = (self.connected and not retained and data.get("online") is True and
                    data.get("ready") is True and time.monotonic() - received <= config.PRESSURE_STALE_AFTER_S)
            return {**data, "live": live, "description": (
                f"Mode: {data.get('mode', '?')}; " +
                ("calibrated" if data.get("calibrated") else "uncalibrated: kPa unavailable") +
                ("" if live else "; waiting/stale"))}

    def bubble_summary(self):
        with self.lock:
            now = time.monotonic()
            while self.bubble_events and now - self.bubble_events[0] > 60:
                self.bubble_events.popleft()
            entry = self.messages.get(config.TOPIC_BUBBLE)
            data, received, retained = entry if entry else ({}, 0, True)
            pressure = self.pressure_summary()
            live = (pressure["live"] and pressure.get("mode") in ("bubble", "both") and
                    not retained and self.connected and now - received <= config.PRESSURE_STALE_AFTER_S)
            return {"live": live, "active": data.get("bubble", False),
                    "count": data.get("count", 0), "last_bubble": self.last_bubble,
                    "rate": len(self.bubble_events), "session": data.get("session")}

    def start(self):
        """Start receiving messages in the MQTT client's background thread."""
        client = make_client(f"biodigester-dashboard-{uuid.uuid4().hex[:8]}")
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self.client = client
        client.connect_async(config.BROKER_HOST, config.BROKER_PORT, config.KEEPALIVE_S)
        client.loop_start()

    # Paho calls these methods when a connection or message event occurs.
    # Its callback signatures include arguments we do not need to use here.
    def _on_connect(self, client, userdata, flags, reason_code, *extra):
        with self.lock:
            self.connected = connected_ok(reason_code)
        if not self.connected:
            return

        topics = list(self.sensor_topics)
        topics.extend([
            config.TOPIC_STATUS,
            config.TOPIC_PELTIER_STATE,
            config.TOPIC_FAULT_STATE,
            config.TOPIC_FAULT_EVENT,
            config.TOPIC_SIM_STATE, config.TOPIC_BUBBLE, config.TOPIC_BUBBLE_EVENT,
            config.TOPIC_PRESSURE_STATUS, config.TOPIC_PRESSURE_RAW,
            config.TOPIC_PRESSURE_DROP, config.TOPIC_PRESSURE_THRESHOLD,
        ])
        for topic in topics:
            client.subscribe(topic, qos=1)

    def _on_disconnect(self, *args):
        with self.lock:
            self.connected = False

    def _on_message(self, client, userdata, message):
        self.handle_message(message.topic, message.payload, bool(message.retain))

    def stop(self):
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()

    def publish(self, topic, data, retain=False):
        """Return an error message if sending fails, or None if it succeeds."""
        if not self.client or not self.client.is_connected():
            return "Not connected to the broker. Nothing was sent."
        result = self.client.publish(topic, json.dumps(data), qos=1, retain=retain)
        if result.rc != 0:
            return "Could not send the request. Try again after reconnecting."
        return None

    def set_range(self, low, high):
        problem = check_thresholds(low, high)
        if problem:
            return problem
        if self.controller_status().get("mode") == "OFFLINE":
            return "Wait for a live controller before changing the range."
        error = self.publish(
            config.TOPIC_THRESHOLDS,
            {"low": float(low), "high": float(high)},
            retain=True,
        )
        if error:
            return error
        return "Range request sent."

    def set_fault(self, fault):
        if not config.SHOW_SIM_PANEL:
            return "Simulator controls are disabled."
        if fault not in config.SIM_FAULTS:
            return "Unknown simulator fault."
        error = self.publish(config.TOPIC_SIM_FAULT, {"fault": fault})
        if error:
            return error
        return "Simulator scenario sent."

    def acknowledge_faults(self):
        error = self.publish(config.TOPIC_FAULT_ACK, {"code": "ALL"})
        if error:
            return error
        return "Acknowledgement/reset request sent."
