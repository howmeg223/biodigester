"""Coordinator for controller-side sensing, diagnostics, control and MQTT I/O."""
import json
import math
import threading
import time

import config
from controller_system.fault_manager import FaultManager
from controller_system.heater_monitor import HeaterMonitor
from controller_system.process_monitor import ProcessMonitor
from controller_system.sensor_monitor import TemperatureSensorMonitor
from controller_system.temperature_control import TemperatureController
from mqtt_common import connected_ok, read_json, read_power, send, limit_peltier_power


class BiodigesterController:
    """Connect the monitors, fault manager and heating rule in one control loop."""
    def __init__(self, client, strategy=config.STRATEGY, clock=time.monotonic):
        self.client = client
        self.clock = clock
        self.lock = threading.RLock()
        self.temperature_sensor = TemperatureSensorMonitor()
        self.process_monitor = ProcessMonitor()
        self.heater_monitor = HeaterMonitor()
        self.faults = FaultManager()
        self.temperature_control = TemperatureController(strategy=strategy)
        self.heater_relay_on = None
        self.feedback_received_at = None
        self.started_at = self.clock()
        self.applied_power = 0.0
        self.safety_cutout = False
        self.note = ""
        self.note_id = 0
        self._reset_requested = False
        self._last_summary = None
        self._strategy = strategy
        self._configure_mqtt()

    def _configure_mqtt(self):
        # The broker publishes this "last will" if the controller disconnects
        # unexpectedly, letting the dashboard know the controller is offline.
        self.client.will_set(
            config.TOPIC_STATUS,
            json.dumps({"mode": "OFFLINE", "heating": False,
                        "reason": "The controller has stopped."}),
            qos=1, retain=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, reason_code, *extra):
        if connected_ok(reason_code):
            for topic in (config.TOPIC_TEMPERATURE, config.TOPIC_THRESHOLDS,
                          config.TOPIC_PELTIER_STATE, config.TOPIC_FAULT_ACK):
                client.subscribe(topic, qos=1)
            print("[controller] Connected to the broker.")

    def _on_message(self, client, userdata, msg):
        data = read_json(msg)
        now = self.clock()
        with self.lock:
            if msg.topic == config.TOPIC_TEMPERATURE:
                if getattr(msg, "retain", False):
                    return
                value = self._read_number(data, "temperature")
                self.temperature_sensor.add_reading(value, now)
            elif msg.topic == config.TOPIC_PELTIER_STATE and isinstance(data, dict):
                if getattr(msg, "retain", False):
                    return
                if data.get("online") is False:
                    self.feedback_received_at = None
                    return
                try:
                    power = read_power(data, "applied_power")
                    if not isinstance(data.get("safety_latched"), bool):
                        return
                except ValueError:
                    return
                self.applied_power = power
                self.heater_relay_on = abs(power) >= config.PELTIER_DEADBAND_PERCENT
                self.feedback_received_at = now
                self.safety_cutout = data["safety_latched"]
            elif msg.topic == config.TOPIC_THRESHOLDS:
                self._handle_threshold_request(data)
            elif msg.topic == config.TOPIC_FAULT_ACK and isinstance(data, dict):
                code = data.get("code", "ALL")
                if not isinstance(code, str):
                    return
                acknowledged = self.faults.acknowledge(code, now)
                if acknowledged:
                    self._reset_requested = True
                    self.note = f"Acknowledged: {', '.join(acknowledged)}."
                    self.note_id += 1

    def _handle_threshold_request(self, data):
        if not isinstance(data, dict):
            low = high = None
        else:
            low, high = data.get("low"), data.get("high")
        problem = self.temperature_control.set_thresholds(low, high)
        if problem:
            self.note = f"Range request rejected. {problem}"
        else:
            thresholds = self.temperature_control.thresholds
            self.note = f"Range set to {thresholds.low:g} to {thresholds.high:g} °C."
        self.note_id += 1

    @staticmethod
    def _read_number(data, key):
        try:
            if isinstance(data[key], bool):
                return math.nan
            value = float(data[key])
        except (TypeError, KeyError, ValueError, OverflowError):
            return math.nan
        return value

    def step(self):
        """Check the equipment, choose a heater command and publish the results."""
        now = self.clock()
        with self.lock:
            feedback_fresh = (self.feedback_received_at is not None and
                              now - self.feedback_received_at <= config.ACTUATOR_FEEDBACK_TIMEOUT_S)
            self.faults.update_condition("PELTIER_FEEDBACK_STALE",
                not feedback_fresh and now - self.started_at >= config.ACTUATOR_FEEDBACK_TIMEOUT_S, now)
            # 1. Check the sensor, process and heater. Heater checks use the
            # command from the previous cycle so the hardware has time to respond.
            sensor_health = self.temperature_sensor.evaluate(
                now, heating_expected=self.temperature_control.heating)
            for code, condition in sensor_health.conditions.items():
                self.faults.update_condition(code, condition, now)

            process_conditions = self.process_monitor.evaluate_temperature(sensor_health.value)
            for code, condition in process_conditions.items():
                self.faults.update_condition(code, condition, now)

            heater_conditions = self.heater_monitor.evaluate(
                now=now,
                commanded_on=self.temperature_control.heating,
                relay_reported_on=self.heater_relay_on if feedback_fresh else None,
                temperature_c=sensor_health.value,
                temperature_trustworthy=sensor_health.trustworthy,
                safety_cutout=self.safety_cutout,
                commanded_power=limit_peltier_power(config.PELTIER_HEAT_POWER) if self.temperature_control.heating else 0.0,
                applied_power=self.applied_power if feedback_fresh else None,
            )
            for code, condition in heater_conditions.items():
                self.faults.update_condition(code, condition, now)

            # 2. Trip or reset the Peltier software safety latch when required.
            if self.faults.safety_trip_required():
                send(self.client, config.TOPIC_PELTIER_SAFETY_CMD, {"action": "TRIP"})
                self._reset_requested = False
            elif self._reset_requested and sensor_health.trustworthy and not any(
                    f["inhibit_heating"] and f["code"] != "SAFETY_CUTOUT_TRIPPED"
                    for f in self.faults.active_faults()):
                # A reset is only attempted after acknowledgement and once no
                # active fault still requires the software safety trip.
                send(self.client, config.TOPIC_PELTIER_SAFETY_CMD, {"action": "RESET"})
                self._reset_requested = False

            # 3. Apply normal control only if the fault manager allows heating.
            decision = self.temperature_control.update(
                now,
                sensor_health.value,
                heating_allowed=feedback_fresh and not self.faults.heating_inhibited(),
            )
            send(self.client, config.TOPIC_PELTIER_CMD,
                 {"power": limit_peltier_power(config.PELTIER_HEAT_POWER) if decision.heating else 0.0})

            # 4. Tell the dashboard about fault changes and the current state.
            events = self.faults.pop_events()
            for event in events:
                send(self.client, config.TOPIC_FAULT_EVENT, event)
            active_faults = self.faults.active_faults()
            send(self.client, config.TOPIC_FAULT_STATE,
                 {"faults": active_faults}, retain=True)

            if any(fault["inhibit_heating"] for fault in active_faults):
                mode = "FAULT"
            else:
                mode = "HEATING" if decision.heating else "IDLE"

            thresholds = self.temperature_control.thresholds
            status = {
                "mode": mode,
                "heating": decision.heating,
                "commanded_power": limit_peltier_power(config.PELTIER_HEAT_POWER) if decision.heating else 0.0,
                "reason": decision.reason,
                "temperature": sensor_health.value,
                "temperature_age_s": sensor_health.age_s,
                "low": thresholds.low,
                "high": thresholds.high,
                "strategy": self._strategy,
                "faults": active_faults,
                "note": self.note,
                "note_id": self.note_id,
                "safety_cutout": self.safety_cutout,
            }
            send(self.client, config.TOPIC_STATUS, status, retain=True)

            # Print only when the operating state changes, rather than every cycle.
            summary = (mode, decision.heating,
                       tuple((fault["code"], fault["acknowledged"]) for fault in active_faults),
                       self.safety_cutout)
            if summary != self._last_summary:
                self._last_summary = summary
                fault_text = ", ".join(fault["code"] for fault in active_faults) or "none"
                shown = "unavailable" if sensor_health.value is None else f"{sensor_health.value:.2f} °C"
                print(f"[controller] {mode:<7} temp={shown:<12} heater={'ON' if decision.heating else 'OFF'} "
                      f"faults={fault_text}")
            return status

    def _on_disconnect(self, *args):
        with self.lock:
            self.feedback_received_at = None
            self.temperature_sensor.received_at = None
            self.temperature_control.update(self.clock(), None, heating_allowed=False)

    def shutdown(self):
        print("\n[controller] Stopping: normal heater command OFF.")
        if self.client.is_connected():
            self.client.publish(config.TOPIC_PELTIER_CMD, json.dumps({"power": 0.0}), qos=1)
            last = self.client.publish(
                config.TOPIC_STATUS,
                json.dumps({"mode": "OFFLINE", "heating": False,
                            "reason": "The controller was stopped."}),
                qos=1, retain=True,
            )
            last.wait_for_publish(timeout=2)
