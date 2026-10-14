"""Pressure-drop detector independent of MQTT and GPIO."""
import statistics
import config


class BubbleDetector:
    def __init__(self):
        self.reset()

    def reset(self):
        self.warmup = []
        self.baseline = None
        self.noise = 1.0
        self.active = False
        self.count = getattr(self, 'count', 0)
        self.candidate_start = None
        self.clear_start = None
        self.trigger_threshold = None

    def update(self, raw, now):
        if self.baseline is None:
            self.warmup.append(raw)
            if len(self.warmup) < config.PRESSURE_WARMUP_SAMPLES:
                return None
            self.baseline = float(statistics.median(self.warmup))
            self.noise = max(float(statistics.pstdev(self.warmup)), 1.0)
            self.warmup = []
        threshold = max(config.PRESSURE_MIN_DROP_COUNTS,
                        self.noise * config.PRESSURE_NOISE_MULTIPLIER)
        drop = self.baseline - raw if config.PRESSURE_DROP_WHEN_RAW_DECREASES else raw - self.baseline
        event = None
        if not self.active:
            if drop >= threshold:
                if self.candidate_start is None:
                    self.candidate_start = now
                    self.trigger_threshold = threshold
                if now - self.candidate_start >= config.PRESSURE_DROP_HOLD_SECONDS:
                    self.active = True
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
                deviation = abs(raw - previous)
                self.noise += config.PRESSURE_NOISE_ALPHA * (deviation - self.noise)
                self.noise = max(self.noise, 1.0)
                self.baseline += config.PRESSURE_BASELINE_ALPHA * (raw - self.baseline)
        elif drop <= self.trigger_threshold * config.PRESSURE_RELEASE_THRESHOLD_RATIO:
            if self.clear_start is None:
                self.clear_start = now
            if now - self.clear_start >= config.PRESSURE_CLEAR_HOLD_SECONDS:
                self.active = False
                self.clear_start = self.candidate_start = None
        else:
            self.clear_start = None
        self.previous_raw = raw
        return {"drop": drop, "threshold": threshold, "event": event}
