# Biodigester monitor

A browser app for temperature and gas pressure, with live readings, history,
reports, heater status, alarms and temperature range controls.
pH has a reserved topic in `config.py` for future implementation.

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

Temperature responds to the heater. Pressure readings are simulated display
examples. The simulator runs 60 times faster than real time; its tank values
are placeholders.

## How it works

- Below 35 °C: request heating. Above 37 °C: request OFF.
- Inside the range, including its boundaries: keep the previous heater state.
- Minimum on/off times delay normal switches. Missing, stale, invalid or frozen
  temperature readings force heating off; process alarms can also stop heating.
- High-high temperature and a stuck heater relay trip an independent safety
  cut-out. Latching faults require acknowledgement after their cause clears.
- The simulated heater trips its safety cut-out if controller commands stop arriving.

Click a sensor card for its history. Use **Report** to compare sensors and choose
a time range. **Save range** validates a request; the active range and controller
reply show its result. Simulator buttons let you silence or freeze the temperature
sensor, send a bad reading, break the heater or clear the injected scenario.
Clearing a scenario does not acknowledge faults or reset the safety cut-out; use
the acknowledgement button to request a safe reset.

Live readings are saved in `data/readings.csv`. Saved MQTT readings are labelled
and not logged again; silent sensors are labelled stale. The dashboard waits for
a live controller update before allowing range changes.
Fault changes are saved in `data/fault_events.csv`.

## Files

| File | Purpose |
|---|---|
| `dashboard.py` | Browser layout, charts and callbacks that use the monitoring service |
| `controller.py` | Starts the controller and runs its control loop |
| `controller_system/` | Sensor checks, process alarms, heater diagnostics, fault tracking and heating rules |
| `monitoring/` | MQTT reception, CSV logging and recent fault notifications |
| `simulator.py` | Starts the simulated hardware |
| `simulation/` | Tank temperature model, simulated devices and test-fault scenarios |
| `config.py` | Broker address, temperature limits and simulation settings |
| `mqtt_common.py` | Shared MQTT connection and message helpers |
| `data/` | Generated sensor and fault-event CSV logs |
| `tests/` | Current service checks plus older tests awaiting migration |
| `control_logic.py`, `fault_handling.py` | Older implementation; unused by the current entry points |

Start reading `config.py`, then `controller_system/temperature_control.py`, then
`controller_system/biodigester_controller.py`. Each project subfolder has a text
guide with file responsibilities, suggested reading order and useful terms:

- [Controller guide](controller_system/controller_system.txt)
- [Monitoring guide](monitoring/monitoring.txt)
- [Simulation guide](simulation/simulation.txt)
- [Data guide](data/data.txt)
- [Tests guide](tests/tests.txt)

The sensor, process and heater monitors report fault conditions. `FaultManager`
tracks those faults and whether heating is allowed. `TemperatureController`
chooses the normal heater command. `BiodigesterController` coordinates these
steps and sends the results over MQTT.

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
`biodigester/sensors/pressure`. `biodigester/sensors/ph` is reserved for later.
The heater receives `{"action": "ON"}` or `{"action": "OFF"}` on
`biodigester/heater/cmd`. It reports relay state, safety cut-out state and effective
power on `biodigester/heater/state`, for example
`{"relay_state": "ON", "safety_cutout": false, "effective_power": "ON"}`.
It must enforce `HEATER_COMMAND_TIMEOUT_S` and accept `TRIP`/`RESET` actions on
`biodigester/heater/safety/cmd`.
Heater commands are sent fresh each cycle; status and range messages are retained.

## Check the code

```bash
python -m pytest -q tests/test_current_services.py
```

These checks exercise the current services without hardware or a broker.
The older `test_control_logic.py` and `test_monitoring.py` still import removed
interfaces, so the full test suite currently fails during collection. The older
live MQTT tests also need updated fault names and heater payloads before use.

If the app shows no readings, check that Mosquitto is running. If it waits for
the controller, use `--demo` or start `controller.py` separately.
