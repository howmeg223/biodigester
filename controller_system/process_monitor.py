"""Process alarms use trustworthy measurements and independent safety limits."""
import config


class ProcessMonitor:
    """Check temperature against process limits defined in config.py."""

    def evaluate_temperature(self, temperature_c):
        # Sensor faults are handled separately; never alarm on an unknown value.
        if temperature_c is None:
            return {
                "LOW_TEMPERATURE": False,
                "HIGH_TEMPERATURE": False,
                "HIGH_HIGH_TEMPERATURE": False,
            }
        return {
            "LOW_TEMPERATURE": temperature_c < config.LOW_TEMP_WARNING_C,
            "HIGH_TEMPERATURE": temperature_c >= config.HIGH_TEMP_C,
            "HIGH_HIGH_TEMPERATURE": temperature_c >= config.HIGH_HIGH_TEMP_C,
        }
