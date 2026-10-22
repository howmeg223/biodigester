# Local bubble rearming and fresh-drop detection

Detector version: `pressure-recovery-v3`.
Based on GitHub main `a604401` (including the current 2,000-count minimum).

## Evidence from the supplied recording

The user observed 21 bubbles. The recording contains 280 raw pressure samples,
21 distinct groups of sharp falls, and seven published bubble events. The session
counter goes from 50 to 57, while recoveries increase from 9 to 13.

For example, raw pressure reaches 1,810,637 against a reference of 1,811,049:
the residual is only 412 counts. The following recorded raw value is 1,799,534
(residual 11,515). Both readings still report the same active bubble/count.
The detector was waiting for a sustained return near its old reference, even
though another real pressure fall had occurred. This merged separate bubbles
into long active periods and repeatedly invoked the ten-second recovery timeout.

## What changed

* Following a counted bubble, track the local trough. Rearm after pressure rises
  by at least the triggering threshold times PRESSURE_RELEASE_THRESHOLD_RATIO,
  for PRESSURE_CLEAR_HOLD_SECONDS. With a 2,000 trigger and 0.40 ratio, that means
  a rise of at least 800 counts from the trough held for 0.2 seconds. It no longer
  requires a sustained return close to the old pre-bubble pressure peak.
* At rearming, reset the reference to the current pressure, avoiding an immediate
  recount of the existing offset. Preserve the running session/count.
* Starting another candidate requires a fresh pressure fall over a 0.25-second
  window, as well as the baseline residual exceeding the adaptive threshold.
  Freeze the window's pre-fall reference for the existing 0.15-second hold check.
  A slow cumulative pressure drift cannot trigger solely by accumulating a large
  distance from an old baseline.
* Window-boundary interpolation accommodates sample timing variation. A gap
  exceeding PRESSURE_READ_TIMEOUT clears the window history and is not treated
  as a sudden pressure fall.
* A held pressure step still counts at most once. The bounded recovery mechanism
  remains available if there is no local recovery. A lid movement can still look
  like one bubble; pressure alone cannot prove the physical cause.
* Existing calibration, GPIO assignments, config.py, Peltier controls, data files,
  dashboard and OLED code are unchanged by this patch.

## Tuning diagnostics

Existing topic `biodigester/diagnostics/pressure/drop` retains the baseline-minus-
raw meaning (with the configured direction). It is a reference offset and can
remain positive while a bubble is active; it is not the size of each new bubble.

NEW topic `biodigester/diagnostics/pressure/window_drop` publishes:

```json
{"drop": 5300, "window_seconds": 0.25}
```

This is the signed pressure change over approximately the last 0.25 seconds.
During pressure rebuild it is negative, on a sudden bubble fall it is positive,
and at a steady pressure it tends to zero even if the held baseline offset is
large. Use it alongside the adaptive threshold and actual event topic:

```bash
mosquitto_sub -h localhost -v -R -t biodigester/diagnostics/pressure/window_drop -t biodigester/diagnostics/pressure/threshold -t biodigester/sensors/bubble -t biodigester/events/bubble
```

A candidate starts when both window drop and baseline residual reach threshold.
It counts only if the fall from its captured reference persists for the configured
hold time. The window signal need not remain above threshold for that entire
hold, since the pre-fall reference is frozen when the candidate starts.

The minimum remains your current config.py setting of 2,000; the adaptive noise
estimate can increase the effective trigger. Set the minimum above quiet-window
noise and below visible real-bubble falls, checking hold duration and manual
counts as well. Do not raise it to the old continuously positive residual.

Optional new setting, with this default supplied internally:

```python
PRESSURE_DROP_WINDOW_SECONDS = 0.25
```

It must be finite and positive. No config.py replacement is included.

## Installation

Upload all ZIP contents into the existing GitHub repository, preserving folders.
Then on the Pi:

```bash
cd ~/biodigester
git pull --ff-only
sudo systemctl restart biodigester@pressure.service
sudo journalctl -u biodigester@pressure.service -n 25 --no-pager -l
```

Startup must show `Bubble detector=pressure-recovery-v3`. Restarting begins a new
hardware session/count, as before. Dashboard/OLED do not need restarting for this
patch. Compare a further minute of visible bubbles against the new session count.

## Verification and limits

The fixture preserves the supplied raw samples and independently identified sharp
fall locations. Tests linearly reconstruct an approximate 40-Hz signal from the
reduced-rate telemetry, using 0.20, 0.215 and 0.25-second telemetry spacings and both
signal directions. All six cases count 21 events at the matching sharp-fall
locations, with no automatic recovery or compensating slow-drift false event.

The recording does not contain original raw-sample timestamps or all ADC samples.
Interpolation and initial rearmed state are explicit replay assumptions, not proof
of perfect hardware detection. Test again on the actual vessel. Additional tests
cover partial pressure recovery without reaching the preceding peak, slow drift,
signed diagnostics, sample gaps, held steps, manual relearning, calibrated pressure
publication and UI coverage states. Full suite: 291 tests passed under Python 3.12 with Dash 4.4.1, pandas 3.0.6 and Plotly 6.9.0; static and whitespace checks passed. An existing MQTT archive test was also corrected to verify its 30 deliberate publications independently of simulator startup messages still in flight.
