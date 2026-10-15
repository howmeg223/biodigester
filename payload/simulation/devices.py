"""Simulated devices deliberately expose hardware behaviour, not controller faults."""
import random
import time

import config
from mqtt_common import read_power, limit_peltier_power, peltier_power_limit, POWER_CONTROL_VERSION


class SimulatedTemperatureSensor:
    """Read tank temperature with noise, or mimic a selected sensor failure."""
    def __init__(self):
        self.mode = "normal"
        self._frozen_value = None

    def set_mode(self, mode, true_temperature=None):
        self.mode = mode
        if mode == "frozen":
            if true_temperature is not None:
                self._frozen_value = float(true_temperature)
        elif mode == "normal":
            self._frozen_value = None

    def read(self, true_temperature):
        if self.mode == "dropout":
            return None
        if self.mode == "out_of_range":
            return config.SIM_OUT_OF_RANGE_C
        if self.mode == "frozen":
            if self._frozen_value is None:
                self._frozen_value = float(true_temperature)
            return self._frozen_value
        return true_temperature + random.gauss(0, config.SIM_SENSOR_NOISE_C)


class SimulatedPressureSensor:
    """Produce demonstration pressure readings with small random changes."""
    def __init__(self, start_kpa=config.SIM_PRESSURE_START_KPA):
        self.value = start_kpa

    def read(self):
        self.value = max(0.0, self.value + random.gauss(0, config.SIM_PRESSURE_NOISE_KPA))
        return self.value


class SimulatedHeater:
    """Normal relay plus a separate safety cut-out.

    `heater_stuck_on` represents a failed/welded normal relay. The independent
    cut-out can still remove heater power. A local command watchdog also trips
    that cut-out if the controller disappears.
    """
    def __init__(self, rated_power_w=config.SIM_HEATER_W, clock=time.monotonic):
        self.rated_power_w = rated_power_w
        self.clock = clock
        self.relay_on = False
        self.requested_power = 0.0
        self.power = 0.0
        self.safety_cutout = False
        self.no_heat_failure = False
        self.stuck_on_failure = False
        self.last_command_time = None

    def command(self, power):
        self.last_command_time = self.clock()
        self.requested_power = read_power({"power": power})
        if self.safety_cutout:
            self.power = 0.0
        else:
            self.power = 100.0 if self.stuck_on_failure else limit_peltier_power(self.requested_power)
        self.relay_on = abs(self.power) >= config.PELTIER_DEADBAND_PERCENT

    def trip_safety(self):
        self.safety_cutout = True

    def reset_safety(self):
        self.safety_cutout = False
        self.power = 0.0
        self.relay_on = False
        self.last_command_time = None

    def set_no_heat_failure(self, enabled):
        self.no_heat_failure = bool(enabled)

    def set_stuck_on_failure(self, enabled):
        self.stuck_on_failure = bool(enabled)
        if enabled:
            self.relay_on = True

    def watchdog(self):
        """Trip the cut-out if commands stop; return True only for a new trip."""
        if self.last_command_time is None:
            return False
        if config.PELTIER_COMMAND_TIMEOUT_S is not None and self.clock() - self.last_command_time > config.PELTIER_COMMAND_TIMEOUT_S:
            if not self.safety_cutout:
                self.trip_safety()
                return True
        return False

    @property
    def delivered_power_w(self):
        """Actual heating power; the relay can be ON even when no heat is delivered."""
        if self.safety_cutout or not self.relay_on or self.no_heat_failure:
            return 0.0
        return self.rated_power_w * self.power / 100.0

    def state_payload(self):
        applied = 0.0 if self.safety_cutout else self.power
        return {"online": True, "requested_power": self.requested_power,
                "applied_power": applied, "simulated": True,
                "driver_version": POWER_CONTROL_VERSION, "max_power": peltier_power_limit(),
                "direction": "POSITIVE" if applied > 0 else "NEGATIVE" if applied < 0 else "OFF",
                "safety_latched": self.safety_cutout, "watchdog_timed_out": False,
                "last_error": None}
