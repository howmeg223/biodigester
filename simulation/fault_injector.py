"""Changes simulated hardware behaviour without telling the controller what failed."""
import config


class FaultInjector:
    """Apply one test scenario to the simulated devices at a time."""

    def __init__(self, temperature_sensor, heater, true_temperature_getter):
        self.temperature_sensor = temperature_sensor
        self.heater = heater
        self.true_temperature_getter = true_temperature_getter
        self.active_fault = "none"

    def apply(self, fault):
        if fault not in config.SIM_FAULTS:
            raise ValueError(f"Unknown simulated fault: {fault}")

        # Return every device to normal first, then apply exactly one scenario.
        self.temperature_sensor.set_mode("normal")
        self.heater.set_no_heat_failure(False)
        self.heater.set_stuck_on_failure(False)

        if fault == "temp_sensor_dropout":
            self.temperature_sensor.set_mode("dropout")
        elif fault == "temp_sensor_frozen":
            self.temperature_sensor.set_mode("frozen", self.true_temperature_getter())
        elif fault == "temp_sensor_out_of_range":
            self.temperature_sensor.set_mode("out_of_range")
        elif fault == "heater_no_heat":
            self.heater.set_no_heat_failure(True)
        elif fault == "heater_stuck_on":
            self.heater.set_stuck_on_failure(True)

        self.active_fault = fault
