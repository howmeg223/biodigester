"""Pressure-drop detector with recovery from sustained baseline disturbances."""
import math
import statistics
from collections import deque
import config

BUBBLE_DETECTOR_VERSION = "pressure-recovery-v3"


class BubbleDetector:
    def __init__(self):
        self.max_active_seconds = self._positive_setting("PRESSURE_MAX_ACTIVE_SECONDS", 10.0)
        self.recovery_seconds = self._positive_setting(
            "PRESSURE_RECOVERY_SECONDS", getattr(config, "PRESSURE_RELEARN_STABLE_SECONDS", 2.0))
        self.rise_alpha = self._positive_setting("PRESSURE_BASELINE_RISE_ALPHA", 0.25)
        if self.rise_alpha > 1:
            raise ValueError("PRESSURE_BASELINE_RISE_ALPHA must be at most 1")
        self.drop_window_seconds = self._positive_setting("PRESSURE_DROP_WINDOW_SECONDS", 0.25)
        self.reset()

    @staticmethod
    def _positive_setting(name, default):
        value = getattr(config, name, default)
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a finite positive number")
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a finite positive number")
        return value

    def reset(self):
        self.warmup = []
        self.baseline = None
        self.noise = 1.0
        self.active = False
        self.count = getattr(self, 'count', 0)
        self.candidate_start = None
        self.candidate_trough = None
        self.trough = None
        self.clear_start = None
        self.trigger_threshold = None
        self.active_since = None
        self.recovering = False
        self.recovery_reason = None
        self.recovery_count = getattr(self, 'recovery_count', 0)
        self._recovery_samples = []
        self._recent_raw = deque()
        if hasattr(self, "previous_raw"):
            del self.previous_raw

    @property
    def ready(self):
        return self.baseline is not None and not self.recovering

    @property
    def state(self):
        if self.recovering:
            return "recovering"
        if self.baseline is None:
            return "warming_up"
        return "active" if self.active else "ready"

    def _threshold(self):
        return max(config.PRESSURE_MIN_DROP_COUNTS,
                   self.noise * config.PRESSURE_NOISE_MULTIPLIER)

    def _drop(self, raw):
        return self.baseline - raw if config.PRESSURE_DROP_WHEN_RAW_DECREASES else raw - self.baseline

    @staticmethod
    def _trough(a, b):
        return min(a, b) if config.PRESSURE_DROP_WHEN_RAW_DECREASES else max(a, b)

    def _rise(self, raw):
        return raw - self.trough if config.PRESSURE_DROP_WHEN_RAW_DECREASES else self.trough - raw

    def _start_recovery(self, raw, now, reason):
        self.recovering = True
        self.recovery_reason = reason
        self.recovery_count += 1
        self.active = False
        self.active_since = self.candidate_start = self.clear_start = None
        self.candidate_trough = self.trough = None
        self.trigger_threshold = None
        self._recovery_samples = []
        return self._recover(raw, now, transition="recovering")

    def _recover(self, raw, now, transition=None):
        # Rebase after a bounded window, including while pressure is changing.
        # Requiring a completely quiet window could disable detection forever.
        # Updating the baseline prevents recounting a held lid-pressure step.
        max_gap = getattr(config, "PRESSURE_READ_TIMEOUT", 1.0)
        if self._recovery_samples and (now <= self._recovery_samples[-1][0] or
                now - self._recovery_samples[-1][0] > max_gap):
            self._recovery_samples = []
        self._recovery_samples.append((now, raw))
        values = [value for _, value in self._recovery_samples]
        if len(values) >= 2 and now - self._recovery_samples[0][0] >= self.recovery_seconds:
            self.baseline = float(raw)
            self.noise = self._sample_noise(values)
            self.previous_raw = raw
            self.recovering = False
            self._recovery_samples = []
            transition = "recovered"
        return {"drop": self._drop(raw), "threshold": self._threshold(),
                "event": None, "transition": transition}

    @staticmethod
    def _sample_noise(values):
        # Successive differences measure local noise without treating the
        # entire warmup/recovery pressure ramp as random noise.
        changes = [abs(b - a) for a, b in zip(values, values[1:])]
        return max(float(statistics.median(changes)), 1.0) if changes else 1.0

    def update(self, raw, now):
        if self._recent_raw and (now <= self._recent_raw[-1][0] or
                now - self._recent_raw[-1][0] > getattr(config, "PRESSURE_READ_TIMEOUT", 1.0)):
            self._recent_raw.clear()
        self._recent_raw.append((now, raw))
        cutoff = now - self.drop_window_seconds
        # Keep the samples bracketing the window boundary. Interpolate that
        # boundary rather than claiming an arbitrarily old reading is recent.
        while len(self._recent_raw) > 2 and self._recent_raw[1][0] <= cutoff:
            self._recent_raw.popleft()
        reference = self._recent_raw[0][1]
        if len(self._recent_raw) >= 2 and self._recent_raw[0][0] < cutoff:
            (t0, v0), (t1, v1) = list(self._recent_raw)[:2]
            reference = v0 + (v1-v0) * (cutoff-t0) / (t1-t0)
        window_drop = reference - raw if config.PRESSURE_DROP_WHEN_RAW_DECREASES else raw - reference
        if self.recovering:
            result = self._recover(raw, now)
            return {**result, "window_drop": window_drop}
        if self.baseline is None:
            self.warmup.append(raw)
            if len(self.warmup) < config.PRESSURE_WARMUP_SAMPLES:
                return None
            self.baseline = float(statistics.median(self.warmup))
            self.noise = self._sample_noise(self.warmup)
            self.warmup = []
        threshold = self._threshold()
        drop = self._drop(raw)
        # Gas accumulation moves pressure opposite to the bubble drop. Track
        # that rise promptly; it is normal operation, not a recovery trigger.
        event = None
        if not self.active:
            if self.candidate_start is None and drop >= threshold and window_drop >= threshold:
                self.candidate_start = now
                self.candidate_trough = raw
                self.trigger_threshold = threshold
                # The fresh fall supplies the pre-bubble reference. A slow
                # historical baseline can lag too far behind a pressure rise.
                self.baseline = float(reference)
                drop = self._drop(raw)
            if self.candidate_start is not None and drop >= self.trigger_threshold:
                self.candidate_trough = self._trough(self.candidate_trough, raw)
                if now - self.candidate_start >= config.PRESSURE_DROP_HOLD_SECONDS:
                    self.active = True
                    self.active_since = now
                    self.trough = self.candidate_trough
                    self.count += 1
                    event = {"event": "bubble", "count": self.count, "raw": raw,
                             "baseline": round(self.baseline), "drop": round(drop),
                             "threshold": round(self.trigger_threshold),
                             "hold_seconds": round(now - self.candidate_start, 3)}
            else:
                self.candidate_start = None
                self.candidate_trough = None
                # Estimate short-term noise from successive residual changes,
                # so gradual baseline drift does not inflate the noise floor.
                previous = getattr(self, 'previous_raw', raw)
                # Bound a single change's contribution: a lid movement is a
                # pressure transient, not evidence of a permanently huge noise
                # floor that would hide the next small bubbles.
                deviation = min(abs(raw - previous), threshold)
                self.noise += config.PRESSURE_NOISE_ALPHA * (deviation - self.noise)
                self.noise = max(self.noise, 1.0)
                change = raw - self.baseline
                if drop < 0:
                    # A single opposite spike must not lift the reference by
                    # tens of thousands and manufacture a subsequent bubble.
                    self.baseline += self.rise_alpha * math.copysign(min(abs(change), threshold), change)
                else:
                    self.baseline += config.PRESSURE_BASELINE_ALPHA * change
        else:
            self.trough = self._trough(self.trough, raw)
            # Rearm from a local pressure recovery, not a return to the old
            # pre-bubble peak. That peak may only be reached just before the
            # next bubble, leaving no time for the release hold to complete.
            if self._rise(raw) >= self.trigger_threshold * config.PRESSURE_RELEASE_THRESHOLD_RATIO:
                if self.clear_start is None:
                    self.clear_start = now
                if now - self.clear_start >= config.PRESSURE_CLEAR_HOLD_SECONDS:
                    self.active = False
                    self.active_since = None
                    self.clear_start = self.candidate_start = None
                    self.candidate_trough = self.trough = self.trigger_threshold = None
                    # Start the next cycle at the current pressure. Keeping
                    # the old peak here would immediately recount this drop.
                    self.baseline = float(raw)
            else:
                self.clear_start = None
        if self.active and self.active_since is not None and now - self.active_since >= self.max_active_seconds:
            result = self._start_recovery(raw, now, "Maximum active pressure-drop duration exceeded")
            return {**result, "window_drop": window_drop}
        self.previous_raw = raw
        return {"drop": self._drop(raw), "window_drop": window_drop,
                "threshold": self._threshold(), "event": event}
