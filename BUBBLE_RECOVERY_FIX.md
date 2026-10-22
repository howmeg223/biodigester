# Pressure baseline recovery

Version: `pressure-recovery-v1`.
Based on GitHub main `7a2ef4a3aa3899d07fe61f30f7dc1623b4a0e828`.

## What caused the stuck reading

The detector froze its reference pressure while a bubble was active. If opening,
pressing or resealing the lid moved the operating pressure permanently, the old
reference could remain tens of thousands of counts away. The detector then waited
indefinitely for pressure to return to that reference.

The reported samples reconstruct the same frozen baseline of 1,846,991 counts.
For example, raw 1,793,120 plus drop 53,871 equals that baseline. The repeatedly
reported 50,000-count drop was this offset, rather than each bubble's amplitude.

## New behaviour

* Normal bubbles retain the existing threshold, hold time and recovery hysteresis.
* A drop that remains active for ten seconds enters baseline recovery. A sustained
  offset in the opposite direction also enters recovery after ten seconds.
* Recovery clears the active indication and suppresses new bubble events. A
  continuously held pressure step is not repeatedly counted at each timeout.
* The sensor must remain in a quiet band for two seconds before a new baseline is
  accepted. The band width is the current trigger threshold multiplied by the
  existing release ratio: 1,120 counts for a 2,800 threshold and 0.40 ratio.
* Movement beyond that band restarts the quiet period. A gap longer than the sensor
  read timeout cannot establish quiet pressure. Recovery waits until readings settle.
* Relearning uses the quiet samples' median, refreshes the noise estimate, and
  resumes normal detection. The running session and count are preserved.
* A single large raw jump's contribution to the adaptive noise estimate is capped
  at the current threshold, reducing sensitivity loss following a lid disturbance.
* Raw ADC and calibrated kPa readings continue throughout recovery. Relearning the
  bubble baseline does not change pressure calibration or the physical zero.

The detector cannot identify the physical cause of a short pressure pulse. A lid
movement can still be counted once before its duration reveals a disturbance;
this patch does not retract an already published event. Use the dashboard's
sampling markers when deliberately opening the vessel. Automatic recovery prevents
the permanent lockup, but it does not reconstruct bubbles missed during settling.

## Files and installation

Upload all supplied files to GitHub with the same folders:

* `pi/bubble_detection.py`
* `pi/pressure.py`
* `tests/test_bubble_recovery.py`
* `BUBBLE_RECOVERY_FIX.md`

No replacement config.py or CSV files are included. Existing 2,800-count
sensitivity, pressure calibration, GPIO pins, temperature and Peltier settings are
preserved. No new packages are required.

Then run on the Pi:

```bash
cd /home/pi/biodigester
git pull --ff-only
sudo systemctl restart biodigester@pressure.service
sudo journalctl -u biodigester@pressure.service --since '2 minutes ago' --no-pager
```

The startup log must show `Bubble detector=pressure-recovery-v1`. Restarting the
service begins a new hardware session and count, as before. Automatic recovery
within a running session preserves that session's count.

## Watch recovery

```bash
mosquitto_sub -h localhost -v \
  -t biodigester/sensors/pressure/status \
  -t biodigester/sensors/bubble \
  -t biodigester/diagnostics/pressure/drop \
  -t biodigester/events/bubble
```

Status now includes `detector_version`, `detector_state`, `recovery_count` and
`recovery_reason` (the last recovery reason). State changes are published even if
they occur between periodic telemetry updates. During recovery, `ready` is false
and `detector_state` is `recovering`; after settling it becomes `ready` with `ready`
true. The bubble payload also includes its state, reference baseline and recovery
count. Service logs announce the beginning and completion of automatic recovery.

Existing dashboard coverage logging treats `ready=false` as unavailable, so quiet
recovery intervals are not presented as confirmed zero bubbling. As before,
recorded events in intervals of incomplete coverage are lower bounds.

The existing manual `RELEARN` command still works and preserves the running count.

## Optional timing overrides

The code provides these defaults even when they are absent from config.py. To
change them, add the following to the existing config.py and restart pressure:

```python
PRESSURE_MAX_ACTIVE_SECONDS = 10.0
PRESSURE_RELEARN_STABLE_SECONDS = 2.0
```

Both must be finite positive numbers. Set the maximum duration longer than a
normal bubble's active pressure pulse. The quiet interval must be short enough to
fit between real bubbles; continuous changes can intentionally keep recovery
waiting. Tune using actual traces rather than increasing the amplitude threshold
to hide a baseline offset.

## Verification

Full suite: 251 tests passed under Python 3.12 with Dash 4.4.1, pandas 3.0.6 and
Plotly 6.9.0. Static checks and patch whitespace checks pass.

Regression tests replay the reported raw values, check both signal directions,
hold a pressure step for thirty seconds without recounting it, detect a subsequent
3,000-count bubble, reject moving pressure as settled, and exercise recovery after
sample gaps. Tests also check manual relearning, preserved counts, recovery status
publication and unchanged calibrated pressure output. Physical lid handling has
not been bench-tested here; confirm the behaviour on the actual vessel.
