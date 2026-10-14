"""Owns fault state, acknowledgement and fault lifecycle events.

Detection does not happen here. Sensor/process/actuator monitors report whether a
condition is currently true; this class turns those conditions into state changes.
"""
from controller_system.models import FaultCategory, FaultDefinition, FaultState, Severity


FAULT_DEFINITIONS = {
    "PELTIER_POWER_MISMATCH": FaultDefinition(
        "PELTIER_POWER_MISMATCH", FaultCategory.ACTUATOR, Severity.ERROR,
        "Reported Peltier power differs from the requested magnitude or direction.",
        "Force zero output and require inspection and acknowledgement.",
        inhibit_heating=True, trip_safety=True, latching=True,
    ),
    "PELTIER_FEEDBACK_STALE": FaultDefinition(
        "PELTIER_FEEDBACK_STALE", FaultCategory.ACTUATOR, Severity.ERROR,
        "No fresh Peltier output report is available.",
        "Heating is disabled until live actuator feedback returns.",
        inhibit_heating=True,
    ),
    "TEMP_SENSOR_NO_DATA": FaultDefinition(
        "TEMP_SENSOR_NO_DATA", FaultCategory.SENSOR, Severity.CRITICAL,
        "No temperature reading has been received.",
        "Heating is disabled until a trustworthy temperature is available.",
        inhibit_heating=True,
    ),
    "TEMP_SENSOR_STALE": FaultDefinition(
        "TEMP_SENSOR_STALE", FaultCategory.SENSOR, Severity.CRITICAL,
        "The temperature sensor has stopped reporting.",
        "Heating is disabled and the operator is notified until readings return.",
        inhibit_heating=True,
    ),
    "TEMP_SENSOR_OUT_OF_RANGE": FaultDefinition(
        "TEMP_SENSOR_OUT_OF_RANGE", FaultCategory.SENSOR, Severity.CRITICAL,
        "The temperature sensor reported an implausible value.",
        "The reading is rejected and heating is disabled.",
        inhibit_heating=True,
    ),
    "TEMP_SENSOR_STUCK": FaultDefinition(
        "TEMP_SENSOR_STUCK", FaultCategory.SENSOR, Severity.CRITICAL,
        "The temperature sensor appears frozen while heating is expected to change the process.",
        "Heating is disabled until the sensor produces trustworthy changing readings.",
        inhibit_heating=True,
    ),
    "LOW_TEMPERATURE": FaultDefinition(
        "LOW_TEMPERATURE", FaultCategory.PROCESS, Severity.WARNING,
        "Digester temperature is below the process warning limit.",
        "Continue monitoring while normal temperature control attempts recovery.",
    ),
    "HIGH_TEMPERATURE": FaultDefinition(
        "HIGH_TEMPERATURE", FaultCategory.PROCESS, Severity.ERROR,
        "Digester temperature is above the high-temperature limit.",
        "Normal heating is forced off and the operator is notified.",
        inhibit_heating=True,
    ),
    "HIGH_HIGH_TEMPERATURE": FaultDefinition(
        "HIGH_HIGH_TEMPERATURE", FaultCategory.PROCESS, Severity.CRITICAL,
        "Digester temperature reached the high-high safety limit.",
        "Trip the Peltier software safety latch and require acknowledgement before reset.",
        inhibit_heating=True, trip_safety=True, latching=True,
    ),
    "HEATER_FAILED_TO_SWITCH_ON": FaultDefinition(
        "HEATER_FAILED_TO_SWITCH_ON", FaultCategory.ACTUATOR, Severity.ERROR,
        "The Peltier output did not reach the commanded ON state.",
        "Stop the heating attempt and require acknowledgement after the actuator is checked.",
        inhibit_heating=True, latching=True,
    ),
    "HEATER_STUCK_ON": FaultDefinition(
        "HEATER_STUCK_ON", FaultCategory.ACTUATOR, Severity.CRITICAL,
        "The Peltier output remained ON after an OFF command.",
        "Trip the Peltier software safety latch and require acknowledgement before reset.",
        inhibit_heating=True, trip_safety=True, latching=True,
    ),
    "HEATER_NO_THERMAL_RESPONSE": FaultDefinition(
        "HEATER_NO_THERMAL_RESPONSE", FaultCategory.ACTUATOR, Severity.ERROR,
        "The heater reports ON but the expected temperature rise was not observed.",
        "Stop the heating attempt and require acknowledgement after the heater is checked.",
        inhibit_heating=True, latching=True,
    ),
    "SAFETY_CUTOUT_TRIPPED": FaultDefinition(
        "SAFETY_CUTOUT_TRIPPED", FaultCategory.SYSTEM, Severity.ERROR,
        "The Peltier software safety latch is tripped.",
        "Heating remains unavailable until the operator acknowledges and requests a safe reset.",
        inhibit_heating=True,
    ),
}


class FaultManager:
    """Remember which faults are active and record each change as an event."""

    def __init__(self, definitions=None):
        definitions = definitions or FAULT_DEFINITIONS
        self._states = {}
        for code, definition in definitions.items():
            self._states[code] = FaultState(definition)
        self._events = []

    def update_condition(self, code, condition, now):
        """Receive one monitor's True/False result and update that fault."""
        state = self._states[code]
        condition = bool(condition)
        previous_condition = state.condition_active
        state.condition_active = condition

        if condition:
            if not state.active:
                state.active = True
                state.acknowledged = False
                state.first_detected = now
                state.last_changed = now
                self._emit("RAISED", state, now)
            elif not previous_condition and state.definition.latching:
                # The physical problem reappeared after apparently becoming safe.
                state.acknowledged = False
                state.last_changed = now
                self._emit("REASSERTED", state, now)
            return

        if not state.active:
            return

        if state.definition.latching and not state.acknowledged:
            # The cause has gone away, but the operator must still acknowledge it.
            if previous_condition:
                state.last_changed = now
                self._emit("CONDITION_CLEARED", state, now)
            return

        self._clear(state, now)

    def acknowledge(self, code, now):
        """Mark one fault (or ALL active faults) as seen by the operator.

        Acknowledgement alone does not clear a problem that is still present.
        """
        if code == "ALL":
            codes = []
            for fault_code, state in self._states.items():
                if state.active:
                    codes.append(fault_code)
        else:
            codes = []
            if code in self._states:
                codes.append(code)

        acknowledged = []
        for fault_code in codes:
            state = self._states[fault_code]
            if not state.active:
                continue
            if not state.acknowledged:
                state.acknowledged = True
                state.last_changed = now
                self._emit("ACKNOWLEDGED", state, now)
            acknowledged.append(fault_code)
            if state.definition.latching and not state.condition_active:
                self._clear(state, now)
        return acknowledged

    def _clear(self, state, now):
        state.active = False
        state.last_changed = now
        self._emit("CLEARED", state, now)
        state.first_detected = None
        state.acknowledged = False

    def _emit(self, event_type, state, now):
        self._events.append({
            "event": event_type,
            "time": now,
            **state.as_dict(),
        })

    def pop_events(self):
        """Return queued events and start a new queue for the next cycle."""
        events = self._events
        self._events = []
        return events

    def active_faults(self):
        return [state.as_dict() for state in self._states.values() if state.active]

    def active_codes(self):
        return {state.definition.code for state in self._states.values() if state.active}

    def heating_inhibited(self):
        for state in self._states.values():
            if state.active and state.definition.inhibit_heating:
                return True
        return False

    def safety_trip_required(self):
        for state in self._states.values():
            if state.active and state.definition.trip_safety:
                return True
        return False

    def highest_severity(self):
        active = [state.definition.severity for state in self._states.values() if state.active]
        if Severity.CRITICAL in active:
            return Severity.CRITICAL
        if Severity.ERROR in active:
            return Severity.ERROR
        if Severity.WARNING in active:
            return Severity.WARNING
        return None
