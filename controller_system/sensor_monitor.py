"""Sensor-specific fault detection kept separate from control decisions."""
from collections import deque
from dataclasses import dataclass
import math

import config


@dataclass
class TemperatureHealth:
    """A checked reading, its age and the results of the sensor fault checks."""
    value: float | None
    age_s: float | None
    trustworthy: bool
    conditions: dict[str, bool]


class TemperatureSensorMonitor:
    """Check for missing, old, implausible or frozen temperature readings."""
    def __init__(self, stale_after_s=config.TEMP_STALE_AFTER_S,
                 plausible_min_c=config.TEMP_PLAUSIBLE_MIN_C,
                 plausible_max_c=config.TEMP_PLAUSIBLE_MAX_C,
                 stuck_window_s=config.TEMP_STUCK_WINDOW_S,
                 stuck_max_span_c=config.TEMP_STUCK_MAX_SPAN_C):
        self.stale_after_s = stale_after_s
        self.plausible_min_c = plausible_min_c
        self.plausible_max_c = plausible_max_c
        self.stuck_window_s = stuck_window_s
        self.stuck_max_span_c = stuck_max_span_c
        self.value = None
        self.received_at = None
        self.samples = deque()
        self._heating_since = None

    def add_reading(self, value, received_at):
        """Save the latest value and keep valid numeric samples for frozen checks."""
        self.value = value
        self.received_at = received_at
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            self.samples.append((received_at, float(value)))
            self._trim(received_at)

    def evaluate(self, now, heating_expected=False):
        """Return a usable fresh, plausible temperature; flatness is diagnostic only."""
        self._trim(now)
        no_data = self.received_at is None or self.value is None
        age = None
        if self.received_at is not None:
            age = now - self.received_at
        stale = (not no_data) and age > self.stale_after_s
        out_of_range = False
        if not no_data and not stale:
            try:
                value = float(self.value)
                out_of_range = (isinstance(self.value, bool) or not math.isfinite(value) or value < self.plausible_min_c
                                or value > self.plausible_max_c)
            except (TypeError, ValueError, OverflowError):
                out_of_range = True

        if not heating_expected or no_data or stale or out_of_range:
            self._heating_since = None
        elif self._heating_since is None:
            self._heating_since = now

        stuck = False
        if self._heating_since is not None:
            samples = [(stamp, value) for stamp, value in self.samples if stamp >= self._heating_since]
            if len(samples) >= 2:
                duration = samples[-1][0] - samples[0][0]
                values = [value for _, value in samples]
                if duration >= self.stuck_window_s and max(values) - min(values) <= self.stuck_max_span_c:
                    stuck = True

        conditions = {
            "TEMP_SENSOR_NO_DATA": no_data,
            "TEMP_SENSOR_STALE": stale,
            "TEMP_SENSOR_OUT_OF_RANGE": out_of_range,
            "TEMP_SENSOR_STUCK": stuck,
        }
        # Repeated fresh values may be real thermal inertia or quantisation.
        # Flatness cannot establish sensor failure, so it does not reject them.
        trustworthy = not (no_data or stale or out_of_range)
        value = None
        if trustworthy:
            value = float(self.value)
        return TemperatureHealth(value, age, trustworthy, conditions)

    def _trim(self, now):
        # Retain slightly more than one diagnostic window so duration can reach the limit.
        keep_after = now - self.stuck_window_s - 2.0
        while self.samples and self.samples[0][0] < keep_after:
            self.samples.popleft()
