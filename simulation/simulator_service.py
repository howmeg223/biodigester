"""MQTT-facing simulated hardware system."""
import threading
import time

import config
from mqtt_common import connected_ok, read_json, send
from simulation.devices import SimulatedHeater, SimulatedPressureSensor, SimulatedTemperatureSensor
from simulation.digester_model import DigesterModel, ambient_temperature
from simulation.fault_injector import FaultInjector


class SimulatorService:
    """Run the tank model and simulated devices, communicating over MQTT."""

    TICK_S = 0.2

    def __init__(self, client, start_c=config.SIM_START_C, speed=config.SIM_SPEED):
        self.client = client
        self.speed = speed
        self.lock = threading.RLock()
        self.model = DigesterModel(
            temp_c=start_c,
            slurry_kg=config.SIM_SLURRY_KG,
            heater_w=config.SIM_HEATER_W,
            ua_w_per_k=config.SIM_UA_W_PER_K,
            heater_lag_s=config.SIM_HEATER_LAG_S,
        )
        self.temperature_sensor = SimulatedTemperatureSensor()
        self.pressure_sensor = SimulatedPressureSensor()
        self.heater = SimulatedHeater()
        self.fault_injector = FaultInjector(
            self.temperature_sensor, self.heater, self._true_temperature)
        self.sim_time_s = 0.0
        self._configure_mqtt()

    def _true_temperature(self):
        # Give the fault injector the current temperature when a sensor freezes.
        return self.model.temp_c

    def _configure_mqtt(self):
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, *extra):
        if connected_ok(reason_code):
            client.subscribe(config.TOPIC_HEATER_CMD, qos=1)
            client.subscribe(config.TOPIC_SAFETY_CMD, qos=1)
            client.subscribe(config.TOPIC_SIM_FAULT, qos=1)
            print("[simulator] Connected. Simulated devices are publishing.")

    def _on_message(self, client, userdata, msg):
        data = read_json(msg)
        if not isinstance(data, dict):
            return
        with self.lock:
            if msg.topic == config.TOPIC_HEATER_CMD:
                action = data.get("action")
                if action in ("ON", "OFF"):
                    self.heater.command(action == "ON")
            elif msg.topic == config.TOPIC_SAFETY_CMD:
                action = data.get("action")
                if action == "TRIP":
                    self.heater.trip_safety()
                elif action == "RESET":
                    self.heater.reset_safety()
            elif msg.topic == config.TOPIC_SIM_FAULT:
                fault = data.get("fault")
                if fault in config.SIM_FAULTS:
                    self.fault_injector.apply(fault)
                    print(f"[simulator] Injected hardware scenario: {fault}")

    def physics_step(self, elapsed_real_s):
        """Advance the model faster than real time using the configured speed."""
        with self.lock:
            elapsed_sim_s = elapsed_real_s * self.speed
            ambient = ambient_temperature(
                self.sim_time_s, config.SIM_AMBIENT_MEAN_C, config.SIM_AMBIENT_SWING_C)
            temperature = self.model.step(
                elapsed_sim_s, self.heater.delivered_power_w, ambient)
            self.sim_time_s += elapsed_sim_s
            return temperature, ambient

    def publish_readings(self, ambient):
        """Send sensor readings and equipment state for this cycle."""
        with self.lock:
            temperature = self.temperature_sensor.read(self.model.temp_c)
            if temperature is not None:
                send(self.client, config.TOPIC_TEMPERATURE,
                     {"temperature": round(temperature, 2)})
            pressure = self.pressure_sensor.read()
            send(self.client, config.TOPIC_PRESSURE, {"pressure": round(pressure, 3)})
            send(self.client, config.TOPIC_HEATER_STATE, self.heater.state_payload(), retain=True)
            send(self.client, config.TOPIC_SIM_STATE, {
                "fault": self.fault_injector.active_fault,
                "ambient": round(ambient, 1),
                "speed": self.speed,
                "true_temperature": round(self.model.temp_c, 2),
            }, retain=True)

    def run(self):
        """Update physics frequently and publish readings at the sensor interval."""
        last_tick = time.monotonic()
        next_reading = last_tick
        count = 0
        try:
            while True:
                time.sleep(self.TICK_S)
                now = time.monotonic()
                elapsed = now - last_tick
                last_tick = now
                if self.heater.watchdog():
                    print("[simulator] Controller command timeout: independent safety cut-out TRIPPED.")
                _, ambient = self.physics_step(elapsed)
                if now >= next_reading:
                    next_reading = now + config.SENSOR_PERIOD_S
                    self.publish_readings(ambient)
                    count += 1
                    if count % 5 == 0:
                        with self.lock:
                            print(
                                f"[simulator] true={self.model.temp_c:6.2f} °C  "
                                f"relay={'ON' if self.heater.relay_on else 'OFF'}  "
                                f"cutout={'TRIPPED' if self.heater.safety_cutout else 'ready'}  "
                                f"fault={self.fault_injector.active_fault}"
                            )
        except KeyboardInterrupt:
            print("\n[simulator] Stopped.")
