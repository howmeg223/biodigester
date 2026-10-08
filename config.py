"""
config.py - every setting for the biodigester control system, in one place.

There is no logic in this file. If a number needs to change for the real
digester, change it here and nowhere else.

Values marked PLACEHOLDER are reasonable guesses so the system runs today.
Replace them with your team's real figures before quoting any results.
"""

# ------------------------------------------------------------------ 
# MQTT broker
BROKER_HOST = "localhost"   # change to raspberry pi
BROKER_PORT = 1883
KEEPALIVE_S = 30




# ------------------------------------------------------------------ 
# Topics
# One topic per kind of message, so nothing has to guess what a message means.
ROOT = "biodigester"
TOPIC_TEMPERATURE = f"{ROOT}/sensors/temperature"   # sensor data goes to the app and controller
TOPIC_HEATER_CMD = f"{ROOT}/heater/cmd"             # controller  -> heater
TOPIC_HEATER_STATE = f"{ROOT}/heater/state"         # heater     -> controller, app
TOPIC_THRESHOLDS = f"{ROOT}/control/thresholds"     # app        -> controller
TOPIC_STATUS = f"{ROOT}/control/status"             # controller -> app
TOPIC_SIM_FAULT = f"{ROOT}/sim/fault"               # app -> simulator (testing only)
TOPIC_SIM_STATE = f"{ROOT}/sim/state"               # simulator -> app (testing only)

# ------------------------------------------------------------------ Regulation
# The band the controller holds. The app can change these while it runs.
DEFAULT_LOW_C = 35.0        # PLACEHOLDER - heating comes on below this
DEFAULT_HIGH_C = 37.0       # PLACEHOLDER - heating goes off above this

# What the app is allowed to ask for. Anything outside this is rejected.
THRESHOLD_MIN_C = 20.0
THRESHOLD_MAX_C = 60.0
MIN_BAND_C = 0.5            # low and high must be at least this far apart

STRATEGY = "hysteresis"     # "hysteresis" or "simple" (single setpoint, for comparison)
CONTROL_PERIOD_S = 1.0      # how often the controller re-decides and re-sends the command

# --- Timers. The simulator runs faster than real life (SIM_SPEED below), so the
# --- demo values are short. The comment on each line gives a real-digester value.
STALE_AFTER_S = 10.0        # no reading for this long = sensor fault.   Real: ~60
MIN_ON_S = 5.0              # heater stays on at least this long.        Real: ~300
MIN_OFF_S = 5.0             # heater stays off at least this long.       Real: ~300
ACTUATOR_TIMEOUT_S = 5.0    # heater must match the command within this. Real: ~30

# A reading outside this range is treated as a broken sensor, not a temperature.
PLAUSIBLE_MIN_C = 0.0
PLAUSIBLE_MAX_C = 70.0

ALARM_MARGIN_C = 2.0        # alarm when this far below low or above high

# ------------------------------------------------------------------ Heater side
# If the heater hears no command for this long it switches itself off, so a
# crashed controller or a dead broker can never leave the heater running.
HEATER_COMMAND_TIMEOUT_S = 10.0     # Real: ~60

# ------------------------------------------------------------------ Simulator
SIM_SPEED = 60.0            # simulated seconds per real second (1 = real time)
SENSOR_PERIOD_S = 1.0       # real seconds between published readings
SIM_START_C = 34.0          # digester temperature when the simulator starts

# The simulated digester. ALL PLACEHOLDERS - swap in your proxy's real numbers.
SIM_SLURRY_KG = 20.0        # mass of slurry being heated
SIM_HEATER_W = 150.0        # heater power
SIM_UA_W_PER_K = 2.0        # heat loss per degree above ambient (insulation quality)
SIM_HEATER_LAG_S = 180.0    # how slowly the heater's output rises and fades (0 = instant)
SIM_SENSOR_NOISE_C = 0.08   # random scatter on each reading (standard deviation)
SIM_AMBIENT_MEAN_C = 14.0   # average outdoor temperature
SIM_AMBIENT_SWING_C = 6.0   # day/night swing either side of the average
SIM_BAD_READING_C = -127.0  # value published while the "bad reading" fault is active

# ------------------------------------------------------------------ App
APP_HISTORY_S = 300         # seconds of history shown on the graph
SHOW_SIM_PANEL = True       # the fault buttons. Set False on the real digester.
