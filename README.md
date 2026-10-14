# Biodigester controller and Raspberry Pi hardware

The Pi reads a DS18B20 temperature probe and an HX710B pressure module, drives a
BTS7960 Peltier H-bridge, and can show live status on a Jaycar XC3728 OLED.
The controller makes temperature decisions locally. The browser and OLED monitor
those decisions; neither is needed for the control loop to run.

## Install and run

Use Python 3.10 or later. On Raspberry Pi OS, install Mosquitto and enable 1-Wire
and SPI with `sudo raspi-config`, then reboot. The normal 1-Wire overlay uses
GPIO4. If your working probe uses a different overlay pin, keep that overlay;
`temp.py` reads Linux sysfs and does not change the 1-Wire pin itself.

```bash
sudo apt install mosquitto mosquitto-clients python3-venv python3-dev build-essential swig
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-pi.txt
```

Your user needs access to `/dev/gpiochip0` and `/dev/spidev0.0` (normally the
`gpio` and `spi` groups). Select the appropriate GPIO chip in `config.py` if your
OS enumerates it differently. Configure MQTT credentials there if required.

From the repository root, start each process in its own terminal:

```bash
python -m pi.temp
python -m pi.pressure --mode both
python -m pi.peltier
python controller.py
python dashboard.py
python -m pi.oled
```

`python pi/temp.py`, `python pi/pressure.py`, `python pi/peltier.py` and
`python pi/oled.py` also work. Stop with Ctrl+C or SIGTERM. The actuator starts
with both outputs low and reports status every 0.5 seconds. Temperature is
published every 2 seconds. The dashboard listens on all Pi network interfaces at port 8050. From another
device on the same network, open `http://<PI_IP_ADDRESS>:8050`. Use `hostname -I`
on the Pi to find its address.

For a software demo, install `requirements.txt` and run `python dashboard.py
--demo` against a **separate demo broker**, with the real Pi clients stopped.
The simulator uses the same topics as the hardware. Do not run both publishers
or both actuator services against the same broker. Simulation includes pH
samples; no physical pH sensor driver is included.

## Wiring

See [HARDWARE.md](HARDWARE.md) for the full wiring table, OLED configuration,
calibration, and initial checks. Previous pressure GPIO23/24 and bridge
GPIO18/19 assignments are retained. The repository never recorded the probe's
actual 1-Wire pin; GPIO4 is the Linux default documented here.

## Choose pressure, bubbles, or both

Set `PRESSURE_MODE` in `config.py`, or override it when starting:

```bash
python -m pi.pressure --mode pressure
python -m pi.pressure --mode bubble
python -m pi.pressure --mode both
```

Run **one** pressure process at a time.

- `pressure`: raw diagnostics plus calibrated kPa; no bubble detection.
- `bubble`: pressure-drop detection and events; no kPa publication.
- `both`: both outputs from the same samples. This is the default.

Pressure is monitoring-only; there is no configured pressure trip limit.
Raw telemetry and health status work without calibration. Uncalibrated values
are never labelled as kPa. Set `PRESSURE_ZERO_RAW`, `PRESSURE_CAL_RAW` and
`PRESSURE_CAL_KPA` to enable gauge pressure readings. The existing zero value
1339145 is retained; recheck it if wiring, supply or the sensor changes.

The HX710B sends 24 signed data bits followed by three mode-selection clocks
(27 total), selecting the differential input at nominal 40 samples/second.
Telemetry is limited to five updates/second, while bubble detection processes
every successful conversion. Clock delays beyond the datasheet timing limit
cause the sample to be discarded and the ADC to be reset. Persistent timing
warnings require investigation on the Pi; Python/Linux is not a real-time bus
controller. Missing samples and ADC saturation invalidate detection health.

Bubble detection learns a baseline from 80 samples, rejects brief/noisy drops,
counts a sustained drop once, then requires sustained recovery before counting
another. The threshold combines a minimum count drop and an adaptive noise
estimate. Tune the minimum drop, hold/recovery times and drop direction using
actual bubble traces. A pressure drop alone cannot identify its physical cause;
leaks or valve operations may also create events. The count is per hardware
process session. CSV event history survives restart; the live count does not.
Rate is the number of unique events observed by that monitor in the last 60
seconds, not a calibrated gas flow measurement. QoS duplicates are deduplicated
using the process session and count. Events missed while a monitor is offline
are not reconstructed.

## Temperature and actuator behavior

The existing heating-only hysteresis is retained:

- Below 35 C: request `PELTIER_HEAT_POWER` (default +100%).
- Above 37 C: request zero.
- Inside the band: retain the previous heating request.
- Minimum ON/OFF timers apply to ordinary switching; faults force zero immediately.

The hardware driver supports signed -100 to +100% commands, with both PWM
outputs off for 50 ms before reversal. Automatic cooling/PID is not implemented.
Positive means RPWM, negative means LPWM; check which polarity heats the vessel
before running automatic control. `applied_power` reports the software PWM
setting, **not measured current or proof of delivered heat**.

Heating needs a fresh live temperature and fresh live actuator report. Missing,
stale or implausible temperatures inhibit heating, as do missing actuator
feedback, output mismatch, high temperature and thermal response faults.
The provisional process limits are 40 C high and 42 C high-high. Response and
frozen-sensor observation windows are 600 seconds, replacing the former
12-second demo settings; validate these for the actual vessel and Peltier.

High-high temperature, unexpected output and power mismatch send `TRIP`, which
latches both PWM outputs off. A controller command timeout (10 seconds) or MQTT
disconnect also disables and latches the output. Saved MQTT power/reset commands
are ignored. After the cause is removed, use the dashboard's acknowledgement
button. The controller checks that temperature is trustworthy and inhibiting
faults have cleared before requesting RESET. Reset itself leaves output at zero
until a new power command arrives. A normal controller stop sends zero power.

The Pi latch is **software protection**, not an independent hardware cut-out.
It cannot disconnect a failed power transistor or keep controlling after the
Pi loses power. Real independent protection requires separate hardware capable
of removing the Peltier supply. The documented limits are provisional, not
certified operating limits.

## MQTT contracts

All application payloads are JSON objects. Commands/events are non-retained;
state and diagnostics are retained but presented as saved/stale until live
updates arrive. Retained temperature/output reports cannot authorize heating.

| Topic (`biodigester/` prefix) | Payload example |
|---|---|
| `sensors/temperature` | `{"temperature":36.52}` |
| `sensors/pressure` | `{"pressure":2.31}` |
| `sensors/pressure/status` | `{"online":true,"ready":true,"mode":"both","calibrated":true,"error":null}` |
| `diagnostics/pressure/raw` | `{"raw":1500000}` |
| `diagnostics/pressure/drop` | `{"drop":15000}` |
| `diagnostics/pressure/threshold` | `{"threshold":10000}` |
| `sensors/bubble` | `{"bubble":true,"count":52,"session":"unique-process-id","ready":true}` |
| `events/bubble` | `{"event":"bubble","count":52,"session":"unique-process-id","drop":15000,"threshold":10000}` |
| `peltier/cmd` | `{"power":100}` |
| `peltier/state` | `{"online":true,"requested_power":100,"applied_power":100,"direction":"POSITIVE","safety_latched":false,"watchdog_timed_out":false,"last_error":null}` |
| `peltier/safety` | `{"action":"TRIP"}` or `{"action":"RESET"}` |
| `control/thresholds` | `{"low":35,"high":37}` |
| `control/status` | Controller mode, temperature, thresholds and fault list |
| `faults/ack` | `{"code":"ALL"}` |
| `sensors/ph` | `{"ph":5.2}` (monitoring only) |

The old `heater/*`, `temperature`, `digester/*` and `peltier/power` interfaces
are no longer consumed. Remove old retained actuator commands from your broker
before upgrading. All hardware and controller processes must use the same
version of the repository and central config.

## Monitoring and tests

The dashboard shows continuous sensor history, Peltier state, pressure hardware
health, bubble count/rate, fault events and intermittent pH samples. The OLED
shows temperature, pressure, bubble count/rate, actuator output and controller
status. Stale/unknown OLED readings are shown as `--` rather than live values.
Sensor, fault and bubble events are saved separately under `data/`.
pH limits are notification-only and saved in the browser; they never command
the Peltier. Invalid pH readings are rejected and simulated samples labelled.

```bash
python -m pip install -r requirements-test.txt
python -m pytest -q
```

The suite covers pure control rules, monitoring, calibration on either slope,
bubble noise/hold/recovery behavior, mode selection, GPIO pin assignments,
signed ADC conversion, bridge reversal, watchdog/latch/reset, temperature sysfs,
OLED rendering and live MQTT safety behavior. MQTT integration creates a private
broker on an ephemeral localhost port; it never uses the production broker.
Optional OLED constructor tests need `requirements-pi.txt`; GPIO/SPI are mocked.
Physical wiring, ADC clock timing under Pi load and actual heating polarity must
still be verified on the installed hardware.

`control_logic.py` and `fault_handling.py` are legacy pure teaching modules;
`controller.py` uses `controller_system/` for the live controller.

## Reports and manual pH sampling

The dashboard service logs subscribed live measurements while it is running;
it does not retrieve historical broker messages. Retained readings are shown
but are not logged again. Keep the dashboard process running to maintain the
archive (browser tabs can be closed without stopping the server).

Use **Report generator** to select temperature, pressure, bubble rate and/or pH.
Choose a preset period or **Custom dates**, with start/end times in the Pi's
local timezone. The end time includes that minute. Click **Generate / refresh
report** to read the saved files, or download the corresponding report CSV.
The report uses separate axes for each unit and pH remains individual dots.
Large temperature/pressure plots use interval means for display; the CSV export
preserves the original selected samples. No report values come from live cards.

Bubble events are counted into 1, 5, 15 or 60-minute bins and divided by each
bin's duration in minutes. Partial boundary bins use their actual duration.
Confirmed quiet periods give zero; missing pressure hardware coverage and
excluded sampling periods give gaps, not artificial zeros. Older event archives
without coverage can show bins with events, but cannot establish quiet periods.
This is a count rate, not litres of gas or a measured gas flow. Very long ranges
may require a larger interval to limit the number of points.

**Log a manual pH measurement** accepts values from 0 to 14. The measurement
time is optional (blank means now); otherwise use `YYYY-MM-DD HH:MM` in the Pi's
local time. The entry publishes a non-retained JSON object to
`biodigester/sensors/ph` with `source: "manual"`, measurement time and a unique
sample ID. The regular MQTT subscriber validates and logs the echoed message.
Duplicate sample IDs are suppressed, including recent IDs after logger restart.
The success message confirms publication; the pH card updates when it is received.

Before opening the vessel, click **Pause analysis for sampling**. It marks an
exclusion period; it does **not** stop raw recording, alarms, or temperature
control. Manual pH samples are kept for analysis during that period. After
closing the vessel, choose a settling time (default 60 seconds) and click
**Resume after settling**. The dashboard requests a fresh bubble baseline via
`biodigester/sensors/bubble/reset` with `{"action":"RELEARN"}`. The pressure
client clears its detection state and learns a new baseline while preserving
the session count; calibration constants remain unchanged. The request is
non-retained, and the pressure client ignores saved reset commands.

The pause survives dashboard restart and applies to every browser connected to
that dashboard server. Sampling periods are shaded on report graphs and excluded
by default. Select **Include sampling periods in analysis** to inspect the raw
period instead. Relearning and settling are not proof that the physical process
has recovered; choose a settling duration suitable for the vessel and inspect
the pressure trace. A dashboard pause is not a physical isolation procedure.

Archive files:

| File | Contents |
|---|---|
| `data/readings.csv` | Temperature, calibrated pressure and pH; source, sample ID, exclusion flag |
| `data/bubble_events.csv` | Individual unique bubble events |
| `data/bubble_coverage.csv` | Hardware readiness heartbeats and disconnect markers used to distinguish zero from missing data |
| `data/sampling_events.csv` | Pause, resume, settling deadlines and operator notes |
| `data/fault_events.csv` | Fault lifecycle events, including during sampling |

The logger upgrades older three-column readings CSVs atomically, preserving
existing rows. Use one dashboard/logger process per set of archive files;
multiple browser tabs are fine. Older analysis files and existing calibration
settings should be kept when installing an update.

See [DASHBOARD_UPDATE.md](DASHBOARD_UPDATE.md) for installing the analysis update while preserving Pi configuration and existing data.

See [SERVICES.md](SERVICES.md) to start the hardware, controller and dashboard automatically at boot using systemd.
