"""Sensor faults and temperature/heater alarms, separate from heating control."""
import math


# Faults force heating off; alarms report a problem while control continues.
NO_READING = "NO_READING"
SENSOR_STALE = "SENSOR_STALE"
SENSOR_BAD_VALUE = "SENSOR_BAD_VALUE"
TEMP_LOW = "TEMP_LOW"
TEMP_HIGH = "TEMP_HIGH"
HEATER_NOT_RESPONDING = "HEATER_NOT_RESPONDING"

MESSAGES = {
    NO_READING: "No temperature reading has arrived yet. Heating is off.",
    SENSOR_STALE: "The sensor has stopped reporting. Heating is off until readings return.",
    SENSOR_BAD_VALUE: "The sensor reading is not a believable temperature. Heating is off.",
    TEMP_LOW: "Temperature is well below the target range.",
    TEMP_HIGH: "Temperature is well above the target range.",
    HEATER_NOT_RESPONDING: "The heater is not doing what it was told.",
}


class FaultHandler:
    """Check readings and remember how long heater feedback has disagreed."""

    def __init__(self, settings):
        self.settings = settings
        self._mismatch_since = None

    def check_reading(self, temperature, age_s):
        """Return a sensor fault code, or None when the reading is usable."""
        if temperature is None or age_s is None:
            return NO_READING
        if age_s > self.settings.stale_after_s:
            return SENSOR_STALE
        if not math.isfinite(temperature):
            return SENSOR_BAD_VALUE
        if temperature < self.settings.plausible_min_c or temperature > self.settings.plausible_max_c:
            return SENSOR_BAD_VALUE
        return None

    def temperature_alarms(self, temperature, thresholds):
        """Check a usable temperature against the current range and margin."""
        alarms = []
        if temperature < thresholds.low - self.settings.alarm_margin_c:
            alarms.append(TEMP_LOW)
        if temperature > thresholds.high + self.settings.alarm_margin_c:
            alarms.append(TEMP_HIGH)
        return alarms

    def heater_alarms(self, now, commanded_on, reported_on):
        """Alarm after a continuous mismatch; clear the timer on agreement."""
        if reported_on is None or reported_on == commanded_on:
            self._mismatch_since = None
            return []
        if self._mismatch_since is None:
            self._mismatch_since = now
        if now - self._mismatch_since >= self.settings.actuator_timeout_s:
            return [HEATER_NOT_RESPONDING]
        return []
