"""control_logic.py - the regulation logic for the biodigester heater."""

import math
from dataclasses import dataclass, field
import config
from fault_handling import FaultHandler, MESSAGES, TEMP_HIGH


# 1. The on/off rule
@dataclass
class Thresholds:
    """The temperature band to hold, in degrees C."""
    low: float = config.DEFAULT_LOW_C
    high: float = config.DEFAULT_HIGH_C

    @property
    def midpoint(self):
        return (self.low + self.high) / 2


def decide_simple(temp_c, setpoint):
    """Baseline strategy: one threshold."""
    return temp_c < setpoint


def decide_hysteresis(temp_c, heating, th):
    """Two thresholds with a hold zone between them."""
    if temp_c < th.low:
        return True       # too cold: heat
    if temp_c > th.high:
        return False      # warm enough: stop
    return heating        # inside the band: keep doing what we were doing


# Control settings
@dataclass
class Settings:
    """Limits and timers for the Regulator. All times are in seconds."""
    strategy: str = config.STRATEGY
    stale_after_s: float = config.TEMP_STALE_AFTER_S
    plausible_min_c: float = config.TEMP_PLAUSIBLE_MIN_C
    plausible_max_c: float = config.TEMP_PLAUSIBLE_MAX_C
    min_on_s: float = config.MIN_ON_S
    min_off_s: float = config.MIN_OFF_S
    alarm_margin_c: float = config.ALARM_MARGIN_C
    actuator_timeout_s: float = config.ACTUATOR_FEEDBACK_TIMEOUT_S
    threshold_min_c: float = config.THRESHOLD_MIN_C
    threshold_max_c: float = config.THRESHOLD_MAX_C
    min_band_c: float = config.MIN_BAND_C


# Range validation
def check_thresholds(low, high, settings):
    """Return None if (low, high) may be used, otherwise a reason to show the user."""
    if isinstance(low, bool) or isinstance(high, bool):
        return "Both temperatures must be numbers."
    try:
        low, high = float(low), float(high)
    except (TypeError, ValueError):
        return "Both temperatures must be numbers."
    if not (math.isfinite(low) and math.isfinite(high)):
        return "Both temperatures must be numbers."
    if low >= high:
        return "The lower temperature must be below the upper one."
    if high - low < settings.min_band_c:
        return f"The two temperatures must be at least {settings.min_band_c:g} °C apart."
    if low < settings.threshold_min_c or high > settings.threshold_max_c:
        return (f"Both temperatures must be between {settings.threshold_min_c:g} "
                f"and {settings.threshold_max_c:g} °C.")
    return None


# The Regulator
@dataclass
class Decision:
    """What the Regulator decided on one update."""
    heating: bool                   # the command: should the heater be on?
    mode: str                       # "HEATING", "IDLE" or "FAULT"
    fault: str | None            # a fault code, or None
    # Each Decision needs its own alarm list; default_factory creates a fresh one.
    alarms: list[str] = field(default_factory=list)
    reason: str = ""                # one plain sentence explaining the decision


class Regulator:
    """Decides, once per update, whether the heater should be on."""

    def __init__(self, thresholds=None, settings=None):
        if thresholds is None:
            thresholds = Thresholds()
        if settings is None:
            settings = Settings()
        self.th = thresholds           # th = thresholds (the target range)
        self.settings = settings
        self.heating = False            # current command
        self._last_switch = None        # time of the last on/off change
        self.fault_handler = FaultHandler(self.settings)

    def set_thresholds(self, low, high):
        """Apply new thresholds if they are sane."""
        problem = check_thresholds(low, high, self.settings)
        if problem is None:
            self.th = Thresholds(float(low), float(high))
        return problem

    def update(self, now, temp_c, reading_age_s, heater_reported_on=None):
        """Make one decision."""
        settings = self.settings

        # 1. Fail-safe. No trustworthy reading means no heating.
        fault = self.fault_handler.check_reading(temp_c, reading_age_s)
        if fault:
            self._switch(False, now)
            alarms = self.fault_handler.heater_alarms(now, self.heating, heater_reported_on)
            return Decision(False, "FAULT", fault, alarms, MESSAGES[fault])

        # 2. Alarms for a temperature well outside the band.
        alarms = self.fault_handler.temperature_alarms(temp_c, self.th)

        # 3. Work out whether the temperature rule requests heating.
        if settings.strategy == "simple":
            requested_heating = decide_simple(temp_c, self.th.midpoint)
        else:
            requested_heating = decide_hysteresis(temp_c, self.heating, self.th)

        # 4. Apply that request if the timer allows it. Overheating bypasses it.
        if requested_heating == self.heating:
            reason = self._steady_reason(temp_c)
        elif not requested_heating and TEMP_HIGH in alarms:
            self._switch(False, now)
            reason = "Far above the target range: heating forced off."
        elif self._may_switch(now):
            self._switch(requested_heating, now)
            if requested_heating:
                reason = "Below the target range: heating switched on."
            else:
                reason = "Above the target range: heating switched off."
        elif self.heating:
            reason = "Warm enough, waiting out the minimum on time before switching off."
        else:
            reason = "Too cold, waiting out the minimum off time before heating again."

        alarms += self.fault_handler.heater_alarms(now, self.heating, heater_reported_on)
        if self.heating:
            mode = "HEATING"
        else:
            mode = "IDLE"
        return Decision(self.heating, mode, None, alarms, reason)

    def _switch(self, on, now):
        """Change the command and remember when it changed."""
        if on != self.heating:
            self.heating = on
            self._last_switch = now

    def _may_switch(self, now):
        """Has the heater been in its current state for long enough?"""
        if self._last_switch is None:
            return True
        if self.heating:
            minimum_duration = self.settings.min_on_s
        else:
            minimum_duration = self.settings.min_off_s
        time_in_current_state = now - self._last_switch
        return time_in_current_state >= minimum_duration

    def _steady_reason(self, temp_c):
        if temp_c < self.th.low:
            return "Below the target range: heating."
        if temp_c > self.th.high:
            return "Above the target range: heating off."
        if self.heating:
            return "Inside the target range: heating until the top of the range."
        return "Inside the target range: heating off until the bottom of the range."

