# Review of latest main — 15 October 2026

Baseline: `7908424ec10b9ace6b87dfe3e08a1a4af19a9230` (fresh GitHub clone).
Remote main was checked again after testing and remained at this commit.
All changes were made against this baseline; older local application copies were
not used. `config.py`, calibration values, pinouts and the 30% limit are unchanged.

## Corrections

- A malformed sampling resume timestamp could prevent dashboard startup. It now
  reports the archive problem and conservatively retains the previous pause.
- CSV write errors could escape the MQTT callback and freeze live reception.
  They are now reported separately while live updates continue. Failed sampling
  marker writes do not falsely claim that analysis was paused or resumed.
- Damaged CSV history produces a graph error instead of a failed dashboard
  callback. Temperature history no longer depends on the bubble archive being valid.
- History graphs preserve sampling and data gaps instead of drawing misleading
  connections. Exports preserve individual decimal samples; manual pH stays
  included during sampling. Invalid resume timestamps are rejected by reports.
- Numeric-string Peltier feedback is normalized before OLED formatting.
  Dashboard output feedback uses the actuator timeout and marks offline or
  unknown output as stale. Applied power remains the driver-reported duty.
- Notification reads and writes are synchronized. Retained fault acknowledgements
  cannot trigger a controller reset. Simulator feedback retains the original
  requested power while its device model enforces the configured limit.
- Removed obsolete installer, duplicate payload sources and tracked bytecode.
  The duplicates could overwrite the current root implementation.
- Repaired the integration test's missing-installer import. Updated dependency
  declarations, Pillow minimum version and installation instructions.

## Verification

206 tests passed on each dependency set:

| Python | Dash | pandas | Plotly |
|---|---|---|---|
| 3.12 | 3.2.0 | 2.2.3 | 6.3.0 |
| 3.12 | 4.4.1 | 3.0.6 | 6.9.0 |

Tests include actual Dash HTTP callback requests with empty, live, legacy and
invalid archives; pause/resume buttons; decimal CSV downloads; unique bubble
counts, quiet intervals and coverage gaps; controller faults and slow thermal
response; and real local MQTT integration with the actual Peltier driver using
mocked GPIO. The actual SH1106 library is exercised with mocked hardware.
Shell syntax and Git whitespace checks passed. Dependency deprecation warnings
remain; no test failures remain.

## What still needs the Pi

The supplied earlier logs concern pressure and OLED; they do not identify the
current dashboard failure. The dashboard starts and its callbacks pass here.
Please provide `sudo journalctl -u biodigester@dashboard.service -b -n 80 --no-pager`
and say whether the page cannot connect, is blank, or reports callback errors.
Pi Python 3.14, GPIO timing, wiring, actual PWM waveform, pressure calibration,
bubble sensitivity and physical display output have not been bench-tested here.
Passing software tests does not establish those physical results.

## Apply these files

Upload the ZIP contents at the repository root, preserving the folder structure.
The ZIP contains changed source, tests and documentation only; no config, data,
virtual environment or installer is included. On GitHub, also delete:

- `install_power_update.py`
- `payload/` (all seven obsolete source files)
- `controller_system/dashboard.cpython-313.pyc`

Alternatively, the accompanying patch applies all additions, edits and deletions
to the stated baseline. See DASHBOARD_UPDATE.md for dependency installation,
service restart and diagnostic commands. No changes have been pushed to GitHub
or installed on the Pi by this review.
