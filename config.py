"""Central configuration for the biodigester controller, monitor and simulator.

The values in this file are demonstration defaults, not certified operating limits.
Before controlling real equipment, replace the PLACEHOLDER values with limits and
response times validated for the actual digester, sensors, heater and safety hardware.
"""
from pathlib import Path

# --------------------------------------------------------------------------- MQTT
BROKER_HOST = "localhost"
BROKER_PORT = 1883
KEEPALIVE_S = 30

ROOT = "biodigester"
TOPIC_TEMPERATURE = f"{ROOT}/sensors/temperature"
TOPIC_PRESSURE = f"{ROOT}/sensors/pressure"
# Reserved for the future pH sensor. Nothing publishes or controls from it yet.
TOPIC_PH = f"{ROOT}/sensors/ph"

TOPIC_HEATER_CMD = f"{ROOT}/heater/cmd"
TOPIC_HEATER_STATE = f"{ROOT}/heater/state"
TOPIC_SAFETY_CMD = f"{ROOT}/heater/safety/cmd"
TOPIC_THRESHOLDS = f"{ROOT}/control/thresholds"
TOPIC_STATUS = f"{ROOT}/control/status"
TOPIC_FAULT_STATE = f"{ROOT}/faults/state"
TOPIC_FAULT_EVENT = f"{ROOT}/faults/event"
TOPIC_FAULT_ACK = f"{ROOT}/faults/ack"
TOPIC_SIM_FAULT = f"{ROOT}/sim/fault"
TOPIC_SIM_STATE = f"{ROOT}/sim/state"

# --------------------------------------------------------------- Active sensors
# Add pH to this dictionary when a real/simulated pH source is implemented.
SENSORS = {
    "temperature": {
        "topic": TOPIC_TEMPERATURE,
        "label": "Temperature",
        "unit": "°C",
        "description": "Digester slurry temperature used by the local heater controller.",
    },
    "pressure": {
        "topic": TOPIC_PRESSURE,
        "label": "Pressure",
        "unit": "kPa",
        "description": "Gas-space pressure. Currently monitored only; safety limits require hardware validation.",
    },
}

# Explicit extension point so pH can be added without changing the architecture.
FUTURE_SENSORS = {
    "ph": {
        "topic": TOPIC_PH,
        "label": "pH",
        "unit": "pH",
        "description": "Reserved for a future pH sensor implementation.",
    }
}

# ---------------------------------------------------------- Temperature control
DEFAULT_LOW_C = 35.0
DEFAULT_HIGH_C = 37.0
THRESHOLD_MIN_C = 20.0
THRESHOLD_MAX_C = 60.0
MIN_BAND_C = 0.5
STRATEGY = "hysteresis"
CONTROL_PERIOD_S = 1.0
MIN_ON_S = 5.0
MIN_OFF_S = 5.0

# ---------------------------------------------------------- Sensor diagnostics
TEMP_STALE_AFTER_S = 10.0
TEMP_PLAUSIBLE_MIN_C = 0.0
TEMP_PLAUSIBLE_MAX_C = 70.0
TEMP_STUCK_WINDOW_S = 12.0
TEMP_STUCK_MAX_SPAN_C = 0.02

# ---------------------------------------------------------- Process protection
# PLACEHOLDER limits. These are intentionally independent from the normal control band.
LOW_TEMP_WARNING_C = 32.0
HIGH_TEMP_C = 40.0
HIGH_HIGH_TEMP_C = 42.0

# ---------------------------------------------------------- Heater diagnostics
ACTUATOR_FEEDBACK_TIMEOUT_S = 5.0
HEATER_RESPONSE_WINDOW_S = 12.0
HEATER_MIN_RISE_C = 0.08
HEATER_COMMAND_TIMEOUT_S = 10.0

# ------------------------------------------------------------------- Simulator
SIM_SPEED = 60.0
SENSOR_PERIOD_S = 1.0
SIM_START_C = 34.0
SIM_SLURRY_KG = 20.0
SIM_HEATER_W = 150.0
SIM_UA_W_PER_K = 2.0
SIM_HEATER_LAG_S = 180.0
SIM_SENSOR_NOISE_C = 0.08
SIM_AMBIENT_MEAN_C = 14.0
SIM_AMBIENT_SWING_C = 6.0
SIM_OUT_OF_RANGE_C = -127.0
SIM_PRESSURE_START_KPA = 2.0
SIM_PRESSURE_NOISE_KPA = 0.03

SIM_FAULTS = (
    "none",
    "temp_sensor_dropout",
    "temp_sensor_frozen",
    "temp_sensor_out_of_range",
    "heater_no_heat",
    "heater_stuck_on",
)

# ---------------------------------------------------------------- Monitoring app
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_FILE = DATA_DIR / "readings.csv"
FAULT_LOG_FILE = DATA_DIR / "fault_events.csv"
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8050
APP_HISTORY_S = 300
SHOW_SIM_PANEL = True
