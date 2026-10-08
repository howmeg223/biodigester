"""
Print out simulated data

"""

import argparse
import math
import random
import threading
import time

import config
from mqtt_common import connect_and_start, connected_ok, make_client, read_json, send

WATER_CP = 4186.0   # J/(kg.K). Slurry is treated as water.


class DigesterModel:
    def __init__(self, temp_c, slurry_kg, heater_w, ua_w_per_k, heater_lag_s=0.0):
        self.temp_c = temp_c                # slurry temperature right now
        self.slurry_kg = slurry_kg
        self.heater_w = heater_w            # heater power when fully on
        self.ua_w_per_k = ua_w_per_k        # heat lost per degree above ambient
        self.heater_lag_s = heater_lag_s
        self.heat_w = 0.0                   # heat actually reaching the slurry

    def step(self, dt_s, heater_on, ambient_c):
        """Advance the model by dt_s seconds and return the new temperature."""
        if heater_on:
            target_w = self.heater_w
        else:
            target_w = 0.0

        # Heater lag: delivered heat drifts towards the target instead of jumping.
        if self.heater_lag_s > 0:
            # This fraction says how far the heater moves towards its target
            # during this step. A longer lag means a smaller change.
            response_fraction = 1.0 - math.exp(-dt_s / self.heater_lag_s)
            self.heat_w += (target_w - self.heat_w) * response_fraction
        else:
            self.heat_w = target_w

        heat_loss_w = self.ua_w_per_k * (self.temp_c - ambient_c)
        net_heat_w = self.heat_w - heat_loss_w
        heat_capacity = self.slurry_kg * WATER_CP  # energy needed for a 1 °C rise
        temperature_change = net_heat_w / heat_capacity * dt_s
        self.temp_c += temperature_change
        return self.temp_c


def ambient_temperature(t_s, mean_c, swing_c):
    """Outdoor temperature at simulated time t_s: a smooth 24-hour day/night cycle."""
    seconds_per_day = 86400.0
    day_cycle_angle = 2 * math.pi * t_s / seconds_per_day
    return mean_c + swing_c * math.sin(day_cycle_angle)


FAULTS = ("none", "sensor_dropout", "sensor_bad", "heater_failure")
TICK_S = 0.2        # real seconds between physics updates
# Display examples only; these do not model gas production or chemistry.
DEMO_SENSORS = {"pressure": (2.0, 0.0, 5.0, 0.08), "ph": (7.0, 6.0, 8.0, 0.03)}


class SimulatedHardware:
    """The pretend sensor and heater. Shared between the MQTT thread and the main loop."""

    def __init__(self, start_c):
        # MQTT receives commands in another thread. This lock protects the tank
        # and heater values while the main loop is updating them.
        self.lock = threading.Lock()
        self.model = DigesterModel(temp_c=start_c,
                                   slurry_kg=config.SIM_SLURRY_KG,
                                   heater_w=config.SIM_HEATER_W,
                                   ua_w_per_k=config.SIM_UA_W_PER_K,
                                   heater_lag_s=config.SIM_HEATER_LAG_S)
        self.heater_on = False
        self.last_command_time = None
        self.fault = "none"

    def command(self, on):
        """A command arrived from the controller."""
        with self.lock:
            self.last_command_time = time.monotonic()
            if self.fault == "heater_failure":
                self.heater_on = False          # a broken heater stays off
            else:
                self.heater_on = on

    def set_fault(self, fault):
        """Remember the requested test fault; a broken heater stays off."""
        with self.lock:
            self.fault = fault
            if fault == "heater_failure":
                self.heater_on = False

    def watchdog(self):
        """Heater-side safety: no command for too long means switch off."""
        with self.lock:
            if self.last_command_time is None:
                return False
            silent_for = time.monotonic() - self.last_command_time
            if self.heater_on and silent_for > config.HEATER_COMMAND_TIMEOUT_S:
                self.heater_on = False
                return True
        return False


def main():
    # 1. Read the optional --speed and --start terminal arguments.
    parser = argparse.ArgumentParser(description="Simulated digester on MQTT")
    parser.add_argument("--speed", type=float, default=config.SIM_SPEED,
                        help="simulated seconds per real second")
    parser.add_argument("--start", type=float, default=config.SIM_START_C,
                        help="starting temperature in °C")
    args = parser.parse_args()

    # 2. Set up the pretend hardware and its MQTT message handlers.
    hardware = SimulatedHardware(args.start)
    client = make_client("biodigester-simulator")

    def on_connect(client, userdata, flags, reason_code, *extra):
        """Subscribe again whenever MQTT connects or reconnects."""
        if connected_ok(reason_code):
            client.subscribe(config.TOPIC_HEATER_CMD, qos=1)
            client.subscribe(config.TOPIC_SIM_FAULT, qos=1)
            print("[simulator] Connected. Publishing readings.")

    def on_message(client, userdata, msg):
        """Handle a heater command or a simulator fault request."""
        data = read_json(msg)
        if data is None:
            return
        if msg.topic == config.TOPIC_HEATER_CMD:
            action = data.get("action")
            if action in ("ON", "OFF"):
                hardware.command(action == "ON")
        elif msg.topic == config.TOPIC_SIM_FAULT:
            fault = data.get("fault")
            if fault in FAULTS:
                hardware.set_fault(fault)
                print(f"[simulator] Fault set to: {fault}")

    client.on_connect = on_connect
    client.on_message = on_message
    connect_and_start(client, "simulator")

    # 3. Track simulated time separately from elapsed time on this computer.
    sim_time_s = 0.0
    last_tick = time.monotonic()
    next_reading = last_tick
    last_reported_heater = None
    readings_sent = 0
    values = {key: spec[0] for key, spec in DEMO_SENSORS.items()}

    try:
        # 4. Repeat: check safety, advance the tank, publish readings.
        while True:
            time.sleep(TICK_S)
            now = time.monotonic()
            elapsed_real_s = now - last_tick
            elapsed_simulated_s = elapsed_real_s * args.speed
            last_tick = now

            if hardware.watchdog():
                print("[simulator] No command from the controller: heater switched itself off.")

            with hardware.lock:
                ambient = ambient_temperature(sim_time_s, config.SIM_AMBIENT_MEAN_C,
                                              config.SIM_AMBIENT_SWING_C)
                true_temperature = hardware.model.step(elapsed_simulated_s, hardware.heater_on, ambient)
                heater_on = hardware.heater_on
                fault = hardware.fault
            sim_time_s += elapsed_simulated_s

            # The heater reports its state when it changes, and once per reading.
            if heater_on != last_reported_heater:
                send(client, config.TOPIC_HEATER_STATE,
                     {"state": "ON" if heater_on else "OFF"}, retain=True)
                last_reported_heater = heater_on

            if now >= next_reading:
                next_reading = now + config.SENSOR_PERIOD_S
                send(client, config.TOPIC_HEATER_STATE,
                     {"state": "ON" if heater_on else "OFF"}, retain=True)
                send(client, config.TOPIC_SIM_STATE,
                     {"fault": fault, "ambient": round(ambient, 1), "speed": args.speed},
                     retain=True)

                # The sensor: silent, nonsense, or the true temperature plus noise.
                if fault == "sensor_dropout":
                    pass
                elif fault == "sensor_bad":
                    send(client, config.TOPIC_TEMPERATURE,
                         {"temperature": config.SIM_BAD_READING_C})
                else:
                    reading = true_temperature + random.gauss(0, config.SIM_SENSOR_NOISE_C)
                    send(client, config.TOPIC_TEMPERATURE, {"temperature": round(reading, 2)})

                for key, (_, low, high, step) in DEMO_SENSORS.items():
                    values[key] = min(high, max(low, values[key] + random.gauss(0, step)))
                    send(client, config.SENSORS[key]["topic"], {key: round(values[key], 2)})

                readings_sent += 1
                if readings_sent % 5 == 0:
                    print(f"[simulator] {true_temperature:6.2f} °C   ambient {ambient:5.1f} °C   "
                          f"heater {'ON ' if heater_on else 'OFF'}   fault: {fault}")
    except KeyboardInterrupt:
        print("\n[simulator] Stopped.")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
