"""Regressions from the 21-bubble capture; telemetry is not a full ADC trace."""
import json
from pathlib import Path

import pytest

import config
from pi.bubble_detection import BubbleDetector


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for name, value in {
        'PRESSURE_WARMUP_SAMPLES': 80,
        'PRESSURE_MIN_DROP_COUNTS': 2000,
        'PRESSURE_NOISE_MULTIPLIER': 6.,
        'PRESSURE_DROP_HOLD_SECONDS': .15,
        'PRESSURE_RELEASE_THRESHOLD_RATIO': .4,
        'PRESSURE_CLEAR_HOLD_SECONDS': .2,
        'PRESSURE_MAX_ACTIVE_SECONDS': 10.,
        'PRESSURE_RECOVERY_SECONDS': 2.,
        'PRESSURE_DROP_WINDOW_SECONDS': .25,
        'PRESSURE_BASELINE_RISE_ALPHA': .25,
        'PRESSURE_BASELINE_ALPHA': .002,
        'PRESSURE_NOISE_ALPHA': .01,
        'PRESSURE_DROP_WHEN_RAW_DECREASES': True,
    }.items():
        monkeypatch.setattr(config, name, value, raising=False)


def warmup(detector, baseline):
    for i in range(80):
        detector.update(baseline, i*.025)
    assert detector.ready


@pytest.mark.parametrize('interval', [.2, .215, .25])
@pytest.mark.parametrize('direction', [True, False])
def test_recorded_21_falls_each_count_once_at_adc_rate(monkeypatch, interval, direction):
    capture = json.loads((Path(__file__).parent/'fixtures/pressure_capture_20261022.json').read_text())
    monkeypatch.setattr(config, 'PRESSURE_DROP_WHEN_RAW_DECREASES', direction)
    baseline = capture['initial_baseline']
    raw = capture['raw'] if direction else [2*baseline-v for v in capture['raw']]
    detector = BubbleDetector()
    warmup(detector, baseline)
    # The real v2 capture starts active from a preceding bubble. A correctly
    # rearmed detector is armed at this point. Warmup establishes that state;
    # it does not pretend to reproduce the unavailable pre-capture waveform.
    detector.update(raw[0], 2.)
    events = []
    for i in range(1, len(raw)):
        # Reconstruct a piecewise linear 40-Hz approximation from ~5-Hz
        # telemetry. Exact ADC samples/timestamps were not recorded.
        steps = round(interval/.025)
        for j in range(1, steps+1):
            value = raw[i-1] + (raw[i]-raw[i-1])*j/steps
            result = detector.update(value, 2.+(i-1+j/steps)*interval)
            if result['event']:
                events.append(i)
    assert len(events) == capture['observed_bubbles'] == 21
    # Matching the total is insufficient: a spurious slow-drift event must
    # not compensate for a missed sharp fall elsewhere in the recording.
    for observed_index, fall_index in zip(events, capture['fall_start_indices']):
        assert fall_index <= observed_index <= fall_index+2
    assert detector.recovery_count == 0


@pytest.mark.parametrize('direction', [True, False])
def test_rearms_without_returning_to_old_peak_and_counts_next_fall(monkeypatch, direction):
    monkeypatch.setattr(config, 'PRESSURE_DROP_WHEN_RAW_DECREASES', direction)
    sign = 1 if direction else -1
    detector = BubbleDetector()
    warmup(detector, 100000)
    events = []
    for i in range(180):
        now = 2.+i*.025
        # 10k drop, recovery to only 94k, then another 5k drop. The old
        # 100k baseline is never reached between these real pulses.
        level = 90000 if i < 20 else min(94000, 90000+(i-20)*100) if i < 100 else 89000
        result = detector.update(100000+sign*(level-100000), now)
        if result['event']:
            events.append(result['event'])
    assert [e['count'] for e in events] == [1, 2]
    assert detector.recovery_count == 0


def test_slow_pressure_fall_is_not_a_fresh_bubble():
    detector = BubbleDetector()
    warmup(detector, 100000)
    for i in range(1600):
        result = detector.update(100000-i*5, 2.+i*.025)
        assert result['event'] is None
    assert detector.count == 0 and detector.recovery_count == 0


def test_window_drop_is_signed_and_not_the_held_baseline_offset():
    detector = BubbleDetector()
    warmup(detector, 100000)
    for i in range(20):
        result = detector.update(100000+i*100, 2.+i*.025)
    assert result['window_drop'] < 0
    result = detector.update(90000, 2.5)
    assert result['window_drop'] > 2000
    for i in range(1, 10):
        result = detector.update(90000, 2.5+i*.025)
    assert detector.count == 1
    for i in range(11, 40):
        result = detector.update(90000, 2.5+i*.025)
    assert result['window_drop'] == 0 and result['drop'] > 2000
    assert detector.count == 1  # A held step cannot repeatedly count.


def test_sample_gap_is_not_a_fresh_sharp_fall():
    detector = BubbleDetector()
    warmup(detector, 100000)
    detector.update(100000, 2.)
    result = detector.update(90000, 4.)
    assert result['window_drop'] == 0 and result['event'] is None
    for i in range(1, 20):
        assert detector.update(90000, 4.+i*.025)['event'] is None
    for i in range(20, 40):
        detector.update(85000, 4.+i*.025)
    assert detector.count == 1


@pytest.mark.parametrize('value', [True, 0, -1, float('nan'), float('inf')])
def test_invalid_drop_window_is_rejected(monkeypatch, value):
    monkeypatch.setattr(config, 'PRESSURE_DROP_WINDOW_SECONDS', value)
    with pytest.raises(ValueError):
        BubbleDetector()
