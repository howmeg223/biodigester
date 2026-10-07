"""Project settings. Simulation values are placeholders, not measured tank data."""
from pathlib import Path

# MQTT delivers messages between the dashboard, controller and sensors.
BROKER_HOST = "localhost"
BROKER_PORT = 1883
KEEPALIVE_S = 30
ROOT = "biodigester"
TOPIC_TEMPERATURE = f"{ROOT}/sensors/temperature"
TOPIC_HEATER_CMD = f"{ROOT}/heater/cmd"
TOPIC_HEATER_STATE = f"{ROOT}/heater/state"
TOPIC_THRESHOLDS = f"{ROOT}/control/thresholds"
TOPIC_STATUS = f"{ROOT}/control/status"
TOPIC_SIM_FAULT = f"{ROOT}/sim/fault"
TOPIC_SIM_STATE = f"{ROOT}/sim/state"

# Heating: on below the lower limit, off above the upper limit.
DEFAULT_LOW_C = 35.0
DEFAULT_HIGH_C = 37.0
THRESHOLD_MIN_C = 20.0
THRESHOLD_MAX_C = 60.0
MIN_BAND_C = 0.5
STRATEGY = "hysteresis"
CONTROL_PERIOD_S = 1.0

# Short timers for the demo (all in seconds). Review for real hardware.
STALE_AFTER_S = 10.0
MIN_ON_S = 5.0
MIN_OFF_S = 5.0
ACTUATOR_TIMEOUT_S = 5.0
HEATER_COMMAND_TIMEOUT_S = 10.0
PLAUSIBLE_MIN_C = 0.0
PLAUSIBLE_MAX_C = 70.0
ALARM_MARGIN_C = 2.0

# Simulated tank and weather.
SIM_SPEED = 60.0            # simulated seconds per real second
SENSOR_PERIOD_S = 1.0
SIM_START_C = 34.0
SIM_SLURRY_KG = 20.0
SIM_HEATER_W = 150.0
SIM_UA_W_PER_K = 2.0        # heat lost per degree above ambient
SIM_HEATER_LAG_S = 180.0    # heat output fades gradually after switching off
SIM_SENSOR_NOISE_C = 0.08
SIM_AMBIENT_MEAN_C = 14.0
SIM_AMBIENT_SWING_C = 6.0
SIM_BAD_READING_C = -127.0

# Browser dashboard and live CSV log.
SHOW_SIM_PANEL = True
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8050
LOG_FILE = Path(__file__).resolve().parent / "data" / "readings.csv"
SENSORS = {
    "temperature": {
        "label": "Temperature", "unit": "°C", "topic": TOPIC_TEMPERATURE,
        "description": "Digester temperature. The controller holds the active target range.",
    },
    "pressure": {
        "label": "Gas pressure", "unit": "kPa", "topic": f"{ROOT}/sensors/pressure",
        "description": "Headspace gas pressure. Monitoring only; no pressure control.",
    },
    "ph": {
        "label": "pH", "unit": "pH", "topic": f"{ROOT}/sensors/ph",
        "description": "Digester pH. Monitoring only; no automatic dosing or configured target.",
    },
}
