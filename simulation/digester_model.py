"""Very small thermal model used by the simulator."""
import math

WATER_CP = 4186.0  # J/(kg.K); demonstration model treats slurry as water.


class DigesterModel:
    """Estimate tank temperature from heater input and heat lost to the air.

    Units in names: c = degrees Celsius, kg = kilograms, w = watts,
    s = seconds and k = kelvin (temperature differences match °C differences).
    ua_w_per_k is the heat lost per degree between the tank and surrounding air.
    """
    def __init__(self, temp_c, slurry_kg, heater_w, ua_w_per_k, heater_lag_s=0.0):
        self.temp_c = temp_c
        self.slurry_kg = slurry_kg
        self.heater_w = heater_w
        self.ua_w_per_k = ua_w_per_k
        self.heater_lag_s = heater_lag_s
        self.heat_w = 0.0

    def step(self, dt_s, delivered_heater_w, ambient_c):
        """Advance the tank by dt_s simulated seconds and return its temperature."""
        target_w = float(delivered_heater_w)
        if self.heater_lag_s > 0:
            # Heating changes gradually to mimic a heater that takes time to warm.
            response_fraction = 1.0 - math.exp(-dt_s / self.heater_lag_s)
            self.heat_w += (target_w - self.heat_w) * response_fraction
        else:
            self.heat_w = target_w

        heat_loss_w = self.ua_w_per_k * (self.temp_c - ambient_c)
        net_heat_w = self.heat_w - heat_loss_w
        heat_capacity = self.slurry_kg * WATER_CP
        # Temperature change = energy added / energy needed for a one-degree rise.
        self.temp_c += net_heat_w / heat_capacity * dt_s
        return self.temp_c


def ambient_temperature(t_s, mean_c, swing_c):
    """Mimic a daily air-temperature cycle around the mean temperature."""
    seconds_per_day = 86400.0
    angle = 2 * math.pi * t_s / seconds_per_day
    return mean_c + swing_c * math.sin(angle)
