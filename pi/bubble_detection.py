"""Pressure-drop detector with recovery from sustained baseline disturbances."""
import math
import statistics
import config

BUBBLE_DETECTOR_VERSION = "pressure-recovery-v1"


class BubbleDetector:
    def __init__(self):
        self.max_active_seconds = self._positive_setting("PRESSURE_MAX_ACTIVE_SECONDS", 10.0)
        self.relearn_stable_seconds = self._positive_setting("PRESSURE_RELEARN_STABLE_SECONDS", 2.0)
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
        self.clear_start = None
        self.trigger_threshold = None
        self.active_since = None
        self._opposite_shift_since = None
        self.recovering = False
        self.recovery_reason = None
        self.recovery_count = getattr(self, 'recovery_count', 0)
        self._quiet_samples = []
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

    def _start_recovery(self, raw, now, reason):
        self.recovering = True
        self.recovery_reason = reason
        self.recovery_count += 1
        self.active = False
        self.active_since = self.candidate_start = self.clear_start = None
        self._opposite_shift_since = self.trigger_threshold = None
        self._quiet_samples = []
        return self._recover(raw, now, transition="recovering")

    def _recover(self, raw, now, transition=None):
        # Never rearm against the old baseline or count during recovery. A
        # simple timeout-and-rearm would count the same held drop repeatedly.
        max_gap = getattr(config, "PRESSURE_READ_TIMEOUT", 1.0)
        if self._quiet_samples and (now <= self._quiet_samples[-1][0] or
                now - self._quiet_samples[-1][0] > max_gap):
            self._quiet_samples = []
        self._quiet_samples.append((now, raw))
        values = [value for _, value in self._quiet_samples]
        quiet_span = max(1.0, self._threshold() * config.PRESSURE_RELEASE_THRESHOLD_RATIO)
        if max(values) - min(values) > quiet_span:
            # A fresh movement restarts the settling period. In particular,
            # an open/pressed lid must not be relearned while it is moving.
            self._quiet_samples = [(now, raw)]
        elif len(values) >= 2 and now - self._quiet_samples[0][0] >= self.relearn_stable_seconds:
            self.baseline = float(statistics.median(values))
            self.noise = max(float(statistics.pstdev(values)), 1.0)
            self.previous_raw = raw
            self.recovering = False
            self._quiet_samples = []
            transition = "recovered"
        return {"drop": self._drop(raw), "threshold": self._threshold(),
                "event": None, "transition": transition}

    def update(self, raw, now):
        if self.recovering:
            return self._recover(raw, now)
        if self.baseline is None:
            self.warmup.append(raw)
            if len(self.warmup) < config.PRESSURE_WARMUP_SAMPLES:
                return None
            self.baseline = float(statistics.median(self.warmup))
            self.noise = max(float(statistics.pstdev(self.warmup)), 1.0)
            self.warmup = []
        threshold = self._threshold()
        drop = self._drop(raw)
        # A sustained shift in the other direction can also leave the slow
        # baseline far behind, e.g. when a pressed lid is released again.
        if not self.active and drop <= -threshold:
            if self._opposite_shift_since is None:
                self._opposite_shift_since = now
            elif now - self._opposite_shift_since >= self.max_active_seconds:
                return self._start_recovery(raw, now, "Sustained pressure shift opposite to the bubble direction")
        else:
            self._opposite_shift_since = None
        event = None
        if not self.active:
            if drop >= threshold:
                if self.candidate_start is None:
                    self.candidate_start = now
                    self.trigger_threshold = threshold
                if now - self.candidate_start >= config.PRESSURE_DROP_HOLD_SECONDS:
                    self.active = True
                    self.active_since = now
                    self.count += 1
                    event = {"event": "bubble", "count": self.count, "raw": raw,
                             "baseline": round(self.baseline), "drop": round(drop),
                             "threshold": round(self.trigger_threshold),
                             "hold_seconds": round(now - self.candidate_start, 3)}
            else:
                self.candidate_start = None
                # Estimate short-term noise from successive residual changes,
                # so gradual baseline drift does not inflate the noise floor.
                previous = getattr(self, 'previous_raw', raw)
                # Bound a single change's contribution: a lid movement is a
                # pressure transient, not evidence of a permanently huge noise
                # floor that would hide the next small bubbles.
                deviation = min(abs(raw - previous), threshold)
                self.noise += config.PRESSURE_NOISE_ALPHA * (deviation - self.noise)
                self.noise = max(self.noise, 1.0)
                self.baseline += config.PRESSURE_BASELINE_ALPHA * (raw - self.baseline)
        elif drop <= self.trigger_threshold * config.PRESSURE_RELEASE_THRESHOLD_RATIO:
            if self.clear_start is None:
                self.clear_start = now
            if now - self.clear_start >= config.PRESSURE_CLEAR_HOLD_SECONDS:
                self.active = False
                self.active_since = None
                self.clear_start = self.candidate_start = None
                self.trigger_threshold = None
        else:
            self.clear_start = None
        if self.active and self.active_since is not None and now - self.active_since >= self.max_active_seconds:
            return self._start_recovery(raw, now, "Maximum active pressure-drop duration exceeded")
        self.previous_raw = raw
        return {"drop": self._drop(raw), "threshold": self._threshold(), "event": event}
