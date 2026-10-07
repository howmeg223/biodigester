# Biodigester monitor

A browser app for temperature, gas pressure and pH, with live readings, history,
reports, heater status, alarms and temperature range controls.

## Set up once

Run these commands from this folder (Python 3.10 or newer):

```bash
brew install mosquitto
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run the demo

```bash
source .venv/bin/activate
brew services start mosquitto
python dashboard.py --demo
```

Open **http://localhost:8050**. The dashboard starts the controller and simulator
for you. Press **Ctrl+C** to stop all three. Stop any separately running
controller or simulator before starting this demo.

Temperature responds to the heater. Pressure and pH are simulated display
examples. The simulator runs 60 times faster than real time; its tank values
are placeholders.

## How it works

- Below 35 °C: request heating. Above 37 °C: request OFF.
- Inside the range, including its boundaries: keep the previous heater state.
- Minimum on/off times delay normal switches. Missing, stale or invalid
  temperature readings force heating off; overheating also overrides the timer.
- The heater switches itself off if controller commands stop arriving.

Click a sensor card for its history. Use **Report** to compare sensors and choose
a time range. **Save range** validates a request; the active range and controller
reply show its result. Simulator buttons let you silence the temperature sensor,
send a bad reading, break the heater or clear the fault.

Live readings are saved in `data/readings.csv`. Saved MQTT readings are labelled
and not logged again; silent sensors are labelled stale. The dashboard waits for
a live controller update before allowing range changes.

## Files

| File | Purpose |
|---|---|
| `dashboard.py` | Monitoring app, MQTT readings, CSV logging and reports |
| `controller.py` | Receives readings and sends heating decisions |
| `control_logic.py` | Heating rules, validation, timers and alarms |
| `simulator.py` | Simulated tank, heater and all three demo sensors |
| `config.py` | Broker address, temperature limits and simulation settings |
| `mqtt_common.py` | Shared MQTT connection and message helpers |
| `tests/` | Control, dashboard and live MQTT checks |
| `archive/before-simplification.zip` | Complete source backup, including the old desktop app and experiment tools |

Start reading `config.py`, then `control_logic.py`, then `controller.py`.
MQTT is the message delivery service; its broker passes JSON messages between
programs using addresses called topics. `with lock:` protects shared values
while MQTT receives messages in the background.

## Run components separately or connect hardware

Use separate terminals, each with `.venv` activated:

```bash
python simulator.py
python controller.py
python dashboard.py
```

Here the dashboard starts only itself. Closing it leaves the independent
controller running. For hardware, replace the simulator with your sensor/heater
program, set the broker address and operating settings in `config.py`, and set
`SHOW_SIM_PANEL = False`. Review simulation values and timers for your real tank.
Run one controller and one dashboard process at a time. The browser server
listens on this computer only by default.

Sensor messages are JSON objects such as `{"temperature": 36.42}` on
`biodigester/sensors/temperature`, `{"pressure": 2.43}` on
`biodigester/sensors/pressure` and `{"ph": 7.02}` on `biodigester/sensors/ph`.
The heater receives `{"action": "ON"}` or `{"action": "OFF"}` on
`biodigester/heater/cmd`, reports `{"state": "ON"}` or `{"state": "OFF"}` on
`biodigester/heater/state`, and must enforce `HEATER_COMMAND_TIMEOUT_S`.
Heater commands are sent fresh each cycle; status and range messages are retained.

## Check the code

```bash
python -m pytest -q
```

Live MQTT tests are skipped if the broker is unavailable or a simulator is
already publishing. Stop your demo before running those tests.

If the app shows no readings, check that Mosquitto is running. If it waits for
the controller, use `--demo` or start `controller.py` separately.
# biodigester
