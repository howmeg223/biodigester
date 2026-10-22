# Pressure baseline recovery v2

Version: `pressure-recovery-v2`. Dashboard: `live-archive-v4`.
Based on GitHub main `9829f9bf24de1165af790c9b456067076b4961b2`.

## Why v1 stopped counting

The supplied log says recovery started at 16:53:22 with count zero. In v1,
active-drop recovery follows a counted event, so this was the other trigger:
sustained pressure movement opposite to a bubble drop. Normal gas accumulation
can move pressure in that direction. Treating that rise as a fault was incorrect.

Recovery then required two seconds within a narrow quiet band (1,120 counts
with the existing settings). Ongoing drift or bubbles could keep restarting that
window indefinitely. Meanwhile, raw pressure and kPa publication continued.
The shared monitoring code treated detector `ready=false` as stale pressure,
which explains why the OLED could say stale despite working pressure readings.

## Changes

* Pressure rises no longer trigger recovery. The baseline tracks the direction
  opposite to a bubble more quickly, using alpha 0.25. Each adjustment is bounded
  to limit the effect of an isolated spike. Existing slow tracking in the drop
  direction, amplitude threshold and hold/release settings remain in use.
* Only an active drop lasting ten seconds triggers automatic recovery by default.
* Recovery suppresses events for a two-second sample window, then rebases to the
  latest reading even if pressure is still moving. Movement does not restart this
  window. A real interruption in samples longer than PRESSURE_READ_TIMEOUT, or a
  non-increasing sample timestamp, does restart it.
* Rebasing prevents a held pressure step being recounted after each timeout.
  Count and session are preserved during recovery. A service restart still starts
  a new session and count, as before.
* Warmup and recovery estimate local noise from median successive differences,
  avoiding a noise estimate inflated by the entire pressure ramp.
* Pressure hardware liveness is separate from detector readiness. Live calibrated
  pressure stays visible during detector warmup/recovery. OLED says `relearn` or
  `warming`; dashboard says `relearning baseline` or `warming up`. Real missing,
  retained, disconnected, offline or error status remains unavailable.
* Coverage logging still marks detector-unavailable intervals unavailable; these
  are not reported as confirmed zero bubbling.

No changes to config.py, GPIO assignments, calibration, Peltier settings, data
files or dependencies are included.

## Installation

Upload every file from the ZIP into the existing repository, preserving folders.
Then on the Pi:

```bash
cd /home/pi/biodigester
git pull --ff-only
sudo systemctl restart biodigester@pressure.service biodigester@dashboard.service biodigester@oled.service
sudo journalctl -u biodigester@pressure.service --since '2 minutes ago' --no-pager -l
```

Startup must show `Bubble detector=pressure-recovery-v2`. Refresh the browser;
the dashboard version is `live-archive-v4`.

Watch detector state and actual events:

```bash
mosquitto_sub -h localhost -v -t biodigester/sensors/pressure/status -t biodigester/sensors/bubble -t biodigester/diagnostics/pressure/drop -t biodigester/diagnostics/pressure/threshold -t biodigester/events/bubble
```

A long active drop should produce a recovery warning, then a resumed message
about two seconds later while samples continue. A normal sustained pressure rise
should produce neither recovery warning nor a stale indication.

## Optional settings

Defaults apply without editing config.py. Optional overrides:

```python
PRESSURE_MAX_ACTIVE_SECONDS = 10.0
PRESSURE_RECOVERY_SECONDS = 2.0
PRESSURE_BASELINE_RISE_ALPHA = 0.25
```

Values must be finite and positive; rise alpha must also be at most one. If
PRESSURE_RECOVERY_SECONDS is absent, an existing PRESSURE_RELEARN_STABLE_SECONDS
value is used for compatibility, but it now sets a bounded sample window.

## Limits and verification

The 2,800-count threshold and 0.15-second hold remain configured. A 3,000-count
peak that falls below threshold in less than the hold time is still rejected.
Raw amplitude alone is insufficient to choose sensitivity: inspect pulse duration
as well. Lid movement can resemble a bubble and can still count once. Recovery
cannot reconstruct missed events or prove the vessel is sealed; use the sampling
controls when deliberately opening it.

Tests cover two-minute pressure ramps without recovery, repeated 3,000-count
bubbles meeting the configured hold time, both signal directions, isolated spikes,
held steps, recovery while pressure moves, sample gaps, manual relearning and UI
status/coverage transitions. Software simulation cannot replace a test on the
actual sensor and vessel. Full software suite: 275 tests passed under Python 3.12, Dash 4.4.1, pandas 3.0.6 and Plotly 6.9.0. Static and whitespace checks also passed.
