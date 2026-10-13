"""Normal temperature regulation. Safety/fault detection lives elsewhere."""
from dataclasses import dataclass
import math

import config


@dataclass
class Thresholds:
    """The lower and upper ends of the normal temperature range, in °C."""
    low: float = config.DEFAULT_LOW_C
    high: float = config.DEFAULT_HIGH_C

    @property
    def midpoint(self):
        return (self.low + self.high) / 2


@dataclass
class ControlDecision:
    """The heater command and a sentence explaining it."""
    heating: bool
    reason: str


def check_thresholds(low, high):
    """Return an error message for an invalid range, otherwise return None."""
    try:
        low = float(low)
        high = float(high)
    except (TypeError, ValueError):
        return "Both temperatures must be numbers."
    if not (math.isfinite(low) and math.isfinite(high)):
        return "Both temperatures must be numbers."
    if low >= high:
        return "The lower temperature must be below the upper one."
    if high - low < config.MIN_BAND_C:
        return f"The two temperatures must be at least {config.MIN_BAND_C:g} °C apart."
    if low < config.THRESHOLD_MIN_C or high > config.THRESHOLD_MAX_C:
        return (f"Both temperatures must be between {config.THRESHOLD_MIN_C:g} "
                f"and {config.THRESHOLD_MAX_C:g} °C.")
    return None


class TemperatureController:
    """Choose ON or OFF while respecting minimum times between switches."""
    def __init__(self, thresholds=None, strategy=config.STRATEGY):
        self.thresholds = thresholds or Thresholds()
        self.strategy = strategy
        self.heating = False
        self._last_switch = None

    def set_thresholds(self, low, high):
        problem = check_thresholds(low, high)
        if problem is None:
            self.thresholds = Thresholds(float(low), float(high))
        return problem

    def update(self, now, temperature_c, heating_allowed=True):
        """Make one heating decision using elapsed time in seconds."""
        # Safety overrides the normal rule and any minimum ON time.
        if not heating_allowed or temperature_c is None:
            self._switch(False, now)
            return ControlDecision(False, "Heating disabled by an active safety or diagnostic condition.")

        if self.strategy == "simple":
            # The simple strategy uses one target, halfway between the limits.
            requested = temperature_c < self.thresholds.midpoint
        elif temperature_c < self.thresholds.low:
            requested = True
        elif temperature_c > self.thresholds.high:
            requested = False
        else:
            # Hysteresis: inside the range (including its boundaries), keep the
            # previous command. This avoids frequent ON/OFF switching.
            requested = self.heating

        if requested != self.heating and self._may_switch(now):
            self._switch(requested, now)

        if self.heating:
            if temperature_c < self.thresholds.low:
                reason = "Below the target range: heating."
            else:
                reason = "Inside the target range: holding heat until the upper threshold."
        else:
            if temperature_c > self.thresholds.high:
                reason = "Above the target range: heating off."
            elif requested and not self._may_switch(now):
                reason = "Below target, waiting for minimum off time before restarting."
            else:
                reason = "Inside the target range: heating remains off."
        return ControlDecision(self.heating, reason)

    def _switch(self, on, now):
        if bool(on) != self.heating:
            self.heating = bool(on)
            self._last_switch = now

    def _may_switch(self, now):
        if self._last_switch is None:
            return True
        if self.heating:
            minimum = config.MIN_ON_S
        else:
            minimum = config.MIN_OFF_S
        return now - self._last_switch >= minimum
