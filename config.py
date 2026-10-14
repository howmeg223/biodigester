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
MQTT_USERNAME = None
MQTT_PASSWORD = None

ROOT = "biodigester"
MQTT_RETAIN = True


TOPIC_TEMPERATURE = f"{ROOT}/sensors/temperature"
TOPIC_PRESSURE = f"{ROOT}/sensors/pressure"
# pH is monitoring-only; never used by the heater controller.
TOPIC_PH = f"{ROOT}/sensors/ph"


# This is where the heater commands sit:
TOPIC_PELTIER_CMD =f"{ROOT}/peltier/cmd"
TOPIC_PELTIER_STATE =f"{ROOT}/peltier/state"
# safety latch:
TOPIC_PELTIER_SAFETY_CMD = f"{ROOT}/peltier/safety"


# Controller topics:
TOPIC_THRESHOLDS = f"{ROOT}/control/thresholds"
TOPIC_STATUS = f"{ROOT}/control/status"

TOPIC_FAULT_STATE = f"{ROOT}/faults/state"
TOPIC_FAULT_EVENT = f"{ROOT}/faults/event"
TOPIC_FAULT_ACK = f"{ROOT}/faults/ack"

TOPIC_SIM_FAULT = f"{ROOT}/sim/fault"
TOPIC_SIM_STATE = f"{ROOT}/sim/state"

# --------------------------------------------------------------- Active sensors

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

# pH notification settings (independent of temperature control).
SENSORS["ph"] = {
    "topic": TOPIC_PH, "label": "Last measured pH", "unit": "pH",
    "description": "Intermittent pH measurements; dots show individual samples, not continuous sensing.",
}
FUTURE_SENSORS = {}
DEFAULT_PH_MIN = 4.2
DEFAULT_PH_MAX = 6.0
PH_SAMPLE_PERIOD_S = 10.0  # Real seconds, independent of SIM_SPEED.
PH_STALE_AFTER_S = 30.0
SIM_PH_ENABLED = True  # Disable before publishing real probe measurements.


# Real temp:
TEMP_SENSOR_GLOB = "/sys/bus/w1/devices/28-*"
TEMP_PUBLISH_INTERVAL_S = 2.0
TEMP_MQTT_RETAIN = True

PELTIER_GPIO_CHIP = 0
PELTIER_RPWM_PIN = 18
PELTIER_LPWM_PIN = 19
PELTIER_PWM_FREQUENCY_HZ = 1000
PELTIER_DIRECTION_CHANGE_DELAY_S = 0.050
PELTIER_COMMAND_TIMEOUT_S = 10.0
PELTIER_DEADBAND_PERCENT = 1.0
PELTIER_FEEDBACK_TOLERANCE_PERCENT = 1.0



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

PELTIER_HEAT_POWER = 100.0
PELTIER_IDLE_POWER = 0.0


# ---------------------------------------------------------- Sensor diagnostics
TEMP_STALE_AFTER_S = 10.0
TEMP_PLAUSIBLE_MIN_C = 0.0
TEMP_PLAUSIBLE_MAX_C = 70.0
TEMP_STUCK_WINDOW_S = 600.0
TEMP_STUCK_MAX_SPAN_C = 0.02

# ---------------------------------------------------------- Process protection
# PLACEHOLDER limits. These are intentionally independent from the normal control band.
LOW_TEMP_WARNING_C = 32.0
HIGH_TEMP_C = 40.0
HIGH_HIGH_TEMP_C = 42.0

# ---------------------------------------------------------- Heater diagnostics
ACTUATOR_FEEDBACK_TIMEOUT_S = 5.0
PELTIER_RESPONSE_WINDOW_S = 600.0
PELTIER_MIN_RISE_C = 0.08
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
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8050
APP_HISTORY_S = 300
SHOW_SIM_PANEL = True

# Hardware pin numbers below are BCM GPIO numbers, not header pin numbers.
# DS18B20 is configured by Linux's dtoverlay=w1-gpio (default BCM4).
TEMP_GPIO_PIN = 4
PRESSURE_GPIO_CHIP = 0
PRESSURE_SCK_PIN = 23
PRESSURE_OUT_PIN = 24
PRESSURE_READ_TIMEOUT = 1.0
PRESSURE_EXTRA_CLOCKS = 3  # 27 clocks: differential input, 40 Hz
PRESSURE_MODE = "both"  # "pressure", "bubble", or "both"; CLI can override
PRESSURE_ZERO_RAW = 1338971
PRESSURE_CAL_RAW = 1694608  # raw reading at a known non-zero gauge pressure
PRESSURE_CAL_KPA = 1.5494507  # that known gauge pressure in kPa
PRESSURE_WARMUP_SAMPLES = 80
PRESSURE_MIN_DROP_COUNTS = 10000
PRESSURE_NOISE_MULTIPLIER = 6.0
PRESSURE_DROP_HOLD_SECONDS = 0.150
PRESSURE_RELEASE_THRESHOLD_RATIO = 0.40
PRESSURE_CLEAR_HOLD_SECONDS = 0.250
PRESSURE_BASELINE_ALPHA = 0.002
PRESSURE_NOISE_ALPHA = 0.01
PRESSURE_TELEMETRY_INTERVAL_S = 0.2
PRESSURE_DROP_WHEN_RAW_DECREASES = True
PRESSURE_STALE_AFTER_S = 5.0
TOPIC_PRESSURE_RAW = f"{ROOT}/diagnostics/pressure/raw"
TOPIC_PRESSURE_DROP = f"{ROOT}/diagnostics/pressure/drop"
TOPIC_PRESSURE_THRESHOLD = f"{ROOT}/diagnostics/pressure/threshold"
TOPIC_PRESSURE_STATUS = f"{ROOT}/sensors/pressure/status"
TOPIC_BUBBLE = f"{ROOT}/sensors/bubble"
TOPIC_BUBBLE_EVENT = f"{ROOT}/events/bubble"
BUBBLE_LOG_FILE = DATA_DIR / "bubble_events.csv"

# Jaycar XC3728: SH1106, 128x64, four-wire SPI.
# DC deliberately avoids BCM24 already used by HX710B.
OLED_DRIVER = "sh1106"
OLED_INTERFACE = "spi"  # "i2c" only if the module was configured for I2C
OLED_SPI_PORT = 0
OLED_SPI_DEVICE = 0
OLED_DC_PIN = 25
OLED_RESET_PIN = 27
OLED_GPIO_CHIP = 0
OLED_I2C_PORT = 1
OLED_I2C_ADDRESS = 0x3C
OLED_ROTATE = 0  # 0, 1, 2, 3 quarter turns
OLED_REFRESH_S = 1.0

# Legacy pure Regulator settings (not used by controller.py).
ALARM_MARGIN_C = 2.0
