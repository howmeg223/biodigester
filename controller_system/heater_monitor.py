"""Actuator diagnostics based on relay feedback and physical thermal response."""
import config


class HeaterMonitor:
    """Compare the heater command with relay feedback and temperature rise."""
    def __init__(self):
        self._mismatch_kind = None
        self._mismatch_since = None
        self._heat_started = None
        self._heat_baseline = None

    def evaluate(self, now, commanded_on, relay_reported_on, temperature_c,
                 temperature_trustworthy, safety_cutout):
        """Return fault conditions; the fault manager decides how to track them."""
        failed_on = False
        stuck_on = False

        if relay_reported_on is None or relay_reported_on == commanded_on:
            self._mismatch_kind = None
            self._mismatch_since = None
        else:
            if not commanded_on and relay_reported_on:
                kind = "stuck_on"
            else:
                kind = "failed_on"
            if kind != self._mismatch_kind:
                self._mismatch_kind = kind
                self._mismatch_since = now
            elapsed = now - self._mismatch_since
            if elapsed >= config.ACTUATOR_FEEDBACK_TIMEOUT_S:
                stuck_on = kind == "stuck_on"
                failed_on = kind == "failed_on"

        thermal_failure = False
        # An ON relay does not prove the heater is actually warming the digester.
        effective_heating = commanded_on and relay_reported_on is True and not safety_cutout
        if effective_heating and temperature_trustworthy and temperature_c is not None:
            if self._heat_started is None:
                self._heat_started = now
                self._heat_baseline = temperature_c
            elif now - self._heat_started >= config.HEATER_RESPONSE_WINDOW_S:
                rise = temperature_c - self._heat_baseline
                thermal_failure = rise < config.HEATER_MIN_RISE_C
        else:
            self._heat_started = None
            self._heat_baseline = None

        return {
            "HEATER_FAILED_TO_SWITCH_ON": failed_on,
            "HEATER_STUCK_ON": stuck_on,
            "HEATER_NO_THERMAL_RESPONSE": thermal_failure,
            "SAFETY_CUTOUT_TRIPPED": bool(safety_cutout),
        }
