"""Receive MQTT messages, save readings and send dashboard requests.

MQTT delivers messages by topic (an address such as biodigester/sensors/temperature).
The controller makes heating decisions; this service supplies the dashboard's data.
"""
from collections import deque
import csv
from datetime import datetime, timedelta
from pathlib import Path
import json
import logging
import math
import queue
import threading
import time
import uuid

import config
from controller_system.temperature_control import check_thresholds
from monitoring.data_logger import DataLogger
from monitoring.notification_manager import NotificationManager
from mqtt_common import connected_ok, make_client, read_power

log = logging.getLogger(__name__)


class MonitoringService:
    """Keep the latest readings and controller messages for the dashboard."""

    def __init__(self, reading_file=config.LOG_FILE, fault_file=config.FAULT_LOG_FILE, bubble_file=config.BUBBLE_LOG_FILE, log_enabled=True, sampling_file=None, coverage_file=None):
        # MQTT receives messages in a background thread. The lock prevents it
        # changing shared data while a dashboard callback is reading that data.
        self.lock = threading.RLock()
        self.latest = {}
        self.ph_simulated = False
        self.log_enabled = log_enabled
        self.logging_errors = {}
        self._log_queue = queue.Queue(maxsize=getattr(config, "MONITOR_LOG_QUEUE_SIZE", 10000))
        self._log_thread = None
        self._log_stop = threading.Event()
        self._log_busy_since = None
        self._receiver_error = None
        self._last_message_at = None
        self._sampling_action_lock = threading.Lock()
        self.sampling_file = Path(sampling_file or Path(reading_file).parent / "sampling_events.csv")
        self.coverage_file = Path(coverage_file or Path(reading_file).parent / "bubble_coverage.csv")
        self.sampling_paused = False
        self.sampling_resume_at = None
        self.sampling_reason = ""
        self._last_coverage_at = None
        self._last_coverage_available = None
        self.reading_seen = set()
        self.reading_seen_order = deque()
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
        self._load_analysis_state()

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
                sample_id = data.get("sample_id", "")
                if not isinstance(sample_id, str) or len(sample_id) > 128:
                    return
                if sample_id and sample_id in self.reading_seen:
                    return
                measured_at = timestamp
                source = "manual" if key == "ph" and data.get("source") == "manual" else (
                    "simulated" if data.get("simulated") is True else "sensor")
                if source == "manual" and "measured_at" in data:
                    try:
                        measured_at = datetime.fromisoformat(data["measured_at"])
                        if measured_at.tzinfo:
                            measured_at = measured_at.astimezone().replace(tzinfo=None)
                        if measured_at > timestamp + timedelta(minutes=5):
                            return
                    except (TypeError, ValueError, OverflowError):
                        return
                # Each reading stores: value, calendar time, saved flag, timer time.
                self.latest[key] = (value, measured_at, retained, received_at)
                if key == "ph":
                    self.ph_simulated = data.get("simulated") is True
                if not retained and self.log_enabled:
                    excluded = self.sampling_status()["paused"] and source != "manual"
                    saved = self._submit_log('readings', self.logger.log_reading,
                                            measured_at, key, value, source, sample_id, excluded)
                    if sample_id and saved:
                        self._remember_reading(sample_id)
                return

            if topic == config.TOPIC_FAULT_EVENT:
                if isinstance(data, dict) and isinstance(data.get("code"), str):
                    self.notifications.process(data, timestamp)
                    if self.log_enabled:
                        self._submit_log('faults', self.logger.log_fault_event, timestamp, data)
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
                    self._submit_log('bubbles', self.logger.log_bubble_event, self.bubble_file, timestamp, data)
            elif topic == config.TOPIC_BUBBLE:
                if not isinstance(data, dict) or data.get("bubble") not in (True, False, 0, 1):
                    return
                if not isinstance(data.get("count"), int) or isinstance(data.get("count"), bool) or data["count"] < 0:
                    return
            elif topic == config.TOPIC_PRESSURE_STATUS:
                if not isinstance(data, dict) or not isinstance(data.get("online"), bool):
                    return
                if not retained:
                    available = (data.get("online") is True and data.get("ready") is True and
                                 data.get("mode") in ("bubble", "both"))
                    self._record_coverage(timestamp, available)
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
                        data = dict(data)
                        data["applied_power"] = read_power(data, "applied_power")
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

    def _remember_reading(self, sample_id):
        self.reading_seen.add(sample_id)
        self.reading_seen_order.append(sample_id)
        if len(self.reading_seen_order) > 10000:
            self.reading_seen.discard(self.reading_seen_order.popleft())

    def _load_analysis_state(self):
        if not self.log_enabled:
            return
        try:
            with self.sampling_file.open(newline="", encoding="utf-8") as stream:
                for row in csv.DictReader(stream):
                    if row.get("event") == "PAUSE":
                        self.sampling_paused = True
                        self.sampling_resume_at = None
                    elif row.get("event") == "RESUME":
                        try:
                            resume_at = datetime.fromisoformat(row.get("resume_at") or row.get("timestamp") or '')
                            if resume_at.tzinfo:
                                resume_at = resume_at.astimezone().replace(tzinfo=None)
                        except (ValueError, TypeError, OverflowError) as exc:
                            self._log_error('sampling history', exc)
                            continue
                        self.sampling_paused = False
                        self.sampling_resume_at = resume_at
                    else:
                        self._log_error('sampling history', 'Missing or unknown sampling event in CSV')
                        continue
                    self.sampling_reason = row.get("reason", "")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, csv.Error) as exc:
            self._log_error('sampling history', exc)
        try:
            with self.log_file.open(newline="", encoding="utf-8") as stream:
                ids = deque((row.get("sample_id") for row in csv.DictReader(stream) if row.get("sample_id")), maxlen=10000)
                for sample_id in ids:
                    self._remember_reading(sample_id)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, csv.Error) as exc:
            self._log_error('readings history', exc)

    def _log_error(self, key, error):
        message = str(error)
        with self.lock:
            if self.logging_errors.get(key) != message:
                log.error("CSV %s: %s", key, message)
            self.logging_errors[key] = message

    def _submit_log(self, key, operation, *args):
        # Services started with start() use a dedicated writer. Direct callers
        # without a running service retain synchronous logging semantics.
        if self._log_thread is None:
            return self._write_log(key, operation, *args)
        try:
            self._log_queue.put_nowait((key, operation, args))
        except queue.Full:
            self._log_error('queue', 'CSV queue is full; some new measurements were not saved. Live readings continue.')
            return False
        return True

    def _log_worker(self):
        while not self._log_stop.is_set() or not self._log_queue.empty():
            try:
                key, operation, args = self._log_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            with self.lock:
                self._log_busy_since = time.monotonic()
            try:
                self._write_log(key, operation, *args)
            finally:
                with self.lock:
                    self._log_busy_since = None
                self._log_queue.task_done()

    def _write_log(self, key, operation, *args):
        """A disk error must not kill MQTT reception or hide live measurements."""
        try:
            operation(*args)
        except Exception as exc:
            self._log_error(key, exc)
            return False
        with self.lock:
            self.logging_errors.pop(key, None)
        return True

    def logging_status(self):
        with self.lock:
            errors = dict(self.logging_errors)
            if self._log_busy_since is not None and time.monotonic() - self._log_busy_since > 10:
                errors['writer'] = f"CSV write delayed; {self._log_queue.qsize()} records queued. Live readings continue."
            if self._receiver_error:
                errors['MQTT receiver'] = self._receiver_error
            return errors

    def _record_coverage(self, timestamp, available):
        if not self.log_enabled:
            return
        if (available != self._last_coverage_available or self._last_coverage_at is None or
                (timestamp-self._last_coverage_at).total_seconds() >= 1):
            if self._submit_log('coverage', self.logger.log_coverage, self.coverage_file, timestamp, available):
                self._last_coverage_at = timestamp
                self._last_coverage_available = available

    def sampling_status(self):
        with self.lock:
            now = datetime.now()
            settling = (not self.sampling_paused and self.sampling_resume_at is not None and
                        now < self.sampling_resume_at)
            return {"paused": self.sampling_paused or settling, "settling": settling,
                    "resume_at": self.sampling_resume_at, "reason": self.sampling_reason}

    def set_sampling(self, paused, reason="pH sampling / vessel open", settle_s=0):
        if not isinstance(paused, bool):
            return "Invalid sampling state."
        if isinstance(settle_s, bool) or not isinstance(settle_s, (int, float)) or not math.isfinite(settle_s) or not 0 <= settle_s <= 3600:
            return "Settling time must be between 0 and 3600 seconds."
        if not isinstance(reason, str) or len(reason) > 500:
            return "Use a sampling note of at most 500 characters."
        with self._sampling_action_lock:
            now = datetime.now()
            state = self.sampling_status()
            if paused and self.sampling_paused:
                return "Analysis is already paused."
            if not paused and state["settling"]:
                return "Analysis is already settling; pause again if you need another sampling period."
            if not paused and not state["paused"]:
                return "Analysis is already running."
            resume_at = None if paused else now + timedelta(seconds=settle_s)
            if not self._write_log('sampling', self.logger.log_sampling_event, self.sampling_file,
                    now, "PAUSE" if paused else "RESUME", reason,
                    resume_at.isoformat(timespec="milliseconds") if resume_at else ""):
                return "Sampling state unchanged: could not save the sampling marker. Check CSV logging status."
            with self.lock:
                self.sampling_paused = paused
                self.sampling_resume_at = resume_at
                self.sampling_reason = reason
            if paused:
                return "Sampling period marked. Raw logging and temperature control continue."
            error = self.publish(getattr(config, "TOPIC_BUBBLE_RESET", f"{config.ROOT}/sensors/bubble/reset"), {"action": "RELEARN"}, retain=False)
            return (f"Analysis resumes after {settle_s:g} seconds of settling. " +
                    (f"Bubble baseline reset was not sent: {error}" if error else
                     "Bubble detector baseline relearning requested."))

    def log_manual_ph(self, value, measured_at=None):
        if isinstance(value, bool):
            return "Enter a pH value between 0 and 14."
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return "Enter a pH value between 0 and 14."
        if not math.isfinite(value) or not 0 <= value <= 14:
            return "Enter a pH value between 0 and 14."
        stamp = datetime.now()
        if measured_at:
            try:
                stamp = datetime.fromisoformat(measured_at)
                if stamp.tzinfo:
                    stamp = stamp.astimezone().replace(tzinfo=None)
            except (TypeError, ValueError, OverflowError):
                return "Use measurement time YYYY-MM-DD HH:MM (Pi local time), or leave it blank."
            if stamp > datetime.now() + timedelta(minutes=5):
                return "Measurement time cannot be more than 5 minutes in the future."
        payload = {"ph": value, "source": "manual", "simulated": False,
                   "measured_at": stamp.isoformat(timespec="milliseconds"), "sample_id": uuid.uuid4().hex}
        error = self.publish(config.TOPIC_PH, payload, retain=False)
        if error:
            return error
        return "pH sent to MQTT; the logger records it when the broker delivers the measurement."

    def snapshot(self):
        """Copy shared dictionaries while holding the lock."""
        with self.lock:
            connected = self.connected and (self.client is None or self.client.is_connected())
            return dict(self.latest), dict(self.messages), bool(connected)

    def receiver_status(self):
        with self.lock:
            age = None if self._last_message_at is None else time.monotonic() - self._last_message_at
            return {"last_message_age": age, "error": self._receiver_error}

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
            connected = self.connected and (self.client is None or self.client.is_connected())
            live = (connected and not retained and data.get("online") is True and not data.get("error") and
                    time.monotonic() - received <= config.PRESSURE_STALE_AFTER_S)
            detector_state = data.get("detector_state", "ready" if data.get("ready") else "warming_up")
            return {**data, "live": live, "description": (
                f"Mode: {data.get('mode', '?')}; " +
                ("calibrated" if data.get("calibrated") else "uncalibrated: kPa unavailable") +
                ("; detector " + detector_state.replace("_", " ") if live else "; waiting/stale"))}

    def bubble_summary(self):
        with self.lock:
            now = time.monotonic()
            while self.bubble_events and now - self.bubble_events[0] > 60:
                self.bubble_events.popleft()
            entry = self.messages.get(config.TOPIC_BUBBLE)
            data, received, retained = entry if entry else ({}, 0, True)
            pressure = self.pressure_summary()
            fresh = (pressure["live"] and
                    not retained and self.connected and now - received <= config.PRESSURE_STALE_AFTER_S)
            enabled = pressure.get("mode") in ("bubble", "both")
            live = fresh and enabled and pressure.get("ready") is True and data.get("ready", True) is True
            state = ("stale" if not pressure["live"] else "disabled" if not enabled else
                     "stale" if not fresh else
                     "active" if live and data.get("bubble") else "idle" if live else
                     pressure.get("detector_state", "warming_up"))
            return {"live": live, "state": state, "active": data.get("bubble", False),
                    "count": data.get("count", 0), "last_bubble": self.last_bubble,
                    "rate": len(self.bubble_events), "session": data.get("session")}

    def start(self):
        """Start receiving messages in the MQTT client's background thread."""
        if self.client is not None or self._log_thread is not None:
            raise RuntimeError("Monitoring service is already started")
        if self.log_enabled:
            self._log_stop.clear()
            self._log_thread = threading.Thread(target=self._log_worker,
                                                name="biodigester-csv-writer", daemon=True)
            self._log_thread.start()
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
            self._record_coverage(datetime.now(), False)

    def _on_message(self, client, userdata, message):
        # A processing failure must not terminate Paho's receiving thread and
        # leave the page displaying a permanently frozen "Connected" state.
        with self.lock:
            self._last_message_at = time.monotonic()
        try:
            self.handle_message(message.topic, message.payload, bool(message.retain))
        except Exception as exc:
            with self.lock:
                self._receiver_error = f"Could not process {message.topic}: {exc}"
            log.exception("MQTT message processing failed for %s; receiver continues", message.topic)
        else:
            with self.lock:
                self._receiver_error = None

    def stop(self):
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()
            self.client = None
        with self.lock:
            self.connected = False
        self._log_stop.set()
        if self._log_thread is not None:
            self._log_thread.join(timeout=5)
            if self._log_thread.is_alive():
                log.error("CSV writer did not drain before shutdown; %s records pending",
                          self._log_queue.qsize())
            else:
                self._log_thread = None

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
        if self.client is not None and not self.client.is_connected():
            return "Not connected to the broker. Nothing was sent."
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
