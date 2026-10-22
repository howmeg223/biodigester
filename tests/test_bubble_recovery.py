"""Recover after lid disturbances without recounting the same pressure plateau."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import config
from pi.bubble_detection import BubbleDetector
from pi.pressure import PressureHardwareClient


BASELINE = 1846991


@pytest.fixture(autouse=True)
def detection_settings(monkeypatch):
    for name, value in {
        'PRESSURE_WARMUP_SAMPLES': 4,
        'PRESSURE_MIN_DROP_COUNTS': 2800,
        'PRESSURE_NOISE_MULTIPLIER': 6.0,
        'PRESSURE_DROP_HOLD_SECONDS': .15,
        'PRESSURE_RELEASE_THRESHOLD_RATIO': .4,
        'PRESSURE_CLEAR_HOLD_SECONDS': .2,
        'PRESSURE_MAX_ACTIVE_SECONDS': 1.0,
        'PRESSURE_RELEARN_STABLE_SECONDS': .5,
        'PRESSURE_DROP_WHEN_RAW_DECREASES': True,
    }.items():
        monkeypatch.setattr(config, name, value, raising=False)


def warmup(detector):
    for i in range(4):
        detector.update(BASELINE, i*.025)
    assert detector.ready


def activate(detector, raw, now=1):
    # Samples normally arrive at 40 Hz; establish the pre-drop level just
    # before this synthetic pulse rather than inventing a long sample gap.
    detector.update(detector.previous_raw, now-.025)
    assert detector.update(raw, now)['event'] is None
    event = detector.update(raw, now+.16)['event']
    assert event is not None
    return event


@pytest.mark.parametrize('direction', [True, False])
def test_held_drop_recovers_once_and_next_real_bubble_counts(monkeypatch, direction):
    monkeypatch.setattr(config, 'PRESSURE_DROP_WHEN_RAW_DECREASES', direction)
    sign = 1 if direction else -1
    raw = BASELINE-sign*50000
    detector = BubbleDetector()
    warmup(detector)
    assert activate(detector, raw)['count'] == 1
    transitions = []
    for i in range(300):
        result = detector.update(raw, 1.2+i*.1)
        assert result['event'] is None
        if result.get('transition'):
            transitions.append(result['transition'])
    assert transitions == ['recovering', 'recovered']
    assert detector.ready and not detector.active
    assert detector.baseline == raw and detector.count == 1
    assert abs(result['drop']) < 1
    assert activate(detector, raw-sign*3000, now=32)['count'] == 2
    detector.update(raw, 32.4)
    detector.update(raw, 32.7)
    assert not detector.active


def test_exact_reported_baseline_and_raw_trace(monkeypatch):
    monkeypatch.setattr(config, 'PRESSURE_MAX_ACTIVE_SECONDS', 10.0)
    monkeypatch.setattr(config, 'PRESSURE_RELEARN_STABLE_SECONDS', 2.0)
    detector = BubbleDetector()
    warmup(detector)
    detector.count = 215
    assert activate(detector, 1793120)['count'] == 216
    trace = [1793120, 1793912, 1794442, 1795436, 1796393, 1796363]
    for i, raw in enumerate(trace):
        result = detector.update(raw, 1.2+i*.2)
        assert result['event'] is None
    for i in range(150):
        result = detector.update(1796363, 2.4+i*.1)
        assert result['event'] is None
    # The local pressure rise now rearms without waiting for the old 1.85M
    # baseline. The remaining static offset cannot count again by itself.
    assert detector.count == 216 and detector.recovery_count == 0
    assert detector.ready and not detector.active
    assert abs(detector.baseline - 1796363) < 50


def test_recovery_finishes_while_pressure_is_rising():
    detector = BubbleDetector()
    warmup(detector)
    activate(detector, BASELINE-50000)
    detector.update(BASELINE-50000, 2.2)  # Held step first enters recovery.
    for i in range(20):
        raw = BASELINE-50000 + i*500
        assert detector.update(raw, 2.3+i*.1)['event'] is None
    assert detector.ready and not detector.recovering
    assert detector.count == 1 and detector.recovery_count == 1


def test_missing_samples_cannot_establish_a_stable_recovery_window():
    detector = BubbleDetector()
    warmup(detector)
    raw = BASELINE-50000
    activate(detector, raw)
    detector.update(raw, 2.2)  # Start recovery.
    detector.update(raw, 2.3)
    detector.update(raw, 5.0)  # Gap exceeds the sensor read timeout.
    assert detector.recovering
    for i in range(1, 7):
        detector.update(raw, 5+i*.1)
    assert detector.ready


@pytest.mark.parametrize('direction', [True, False])
def test_sustained_shift_opposite_to_bubble_direction_tracks_without_disabling(monkeypatch, direction):
    monkeypatch.setattr(config, 'PRESSURE_DROP_WHEN_RAW_DECREASES', direction)
    sign = 1 if direction else -1
    raw = BASELINE+sign*50000
    detector = BubbleDetector()
    warmup(detector)
    for i in range(150):
        assert detector.update(raw, 1+i*.1)['event'] is None
    assert detector.count == 0 and detector.ready
    assert detector.recovery_count == 0 and abs(detector.baseline - raw) < 1


def test_normal_bubbles_and_brief_opposite_spikes_do_not_enter_recovery():
    detector = BubbleDetector()
    warmup(detector)
    detector.update(BASELINE+50000, .3)
    detector.update(BASELINE, .4)
    assert activate(detector, BASELINE-4000, now=1)['count'] == 1
    detector.update(BASELINE, 1.4)
    detector.update(BASELINE, 1.7)
    assert not detector.active and detector.recovery_count == 0
    assert activate(detector, BASELINE-4000, now=2)['count'] == 2
    assert detector.recovery_count == 0


@pytest.mark.parametrize('direction', [True, False])
def test_continuous_pressure_rise_for_two_minutes_never_pauses(monkeypatch, direction):
    monkeypatch.setattr(config, 'PRESSURE_DROP_WHEN_RAW_DECREASES', direction)
    sign = 1 if direction else -1
    detector = BubbleDetector()
    # Include normal ramp during the full production-size warmup.
    monkeypatch.setattr(config, 'PRESSURE_WARMUP_SAMPLES', 80)
    for i in range(4800):
        detector.update(BASELINE + sign*i*25, i*.025)
        if i >= 79:
            assert detector.ready and detector.state == 'ready'
    assert detector.count == 0 and detector.recovery_count == 0


@pytest.mark.parametrize('direction', [True, False])
def test_repeated_3000_count_bubbles_with_slow_gas_pressure_rebuild(monkeypatch, direction):
    monkeypatch.setattr(config, 'PRESSURE_DROP_WHEN_RAW_DECREASES', direction)
    monkeypatch.setattr(config, 'PRESSURE_MAX_ACTIVE_SECONDS', 10.)
    sign = 1 if direction else -1
    detector = BubbleDetector()
    warmup(detector)
    # One bubble every three seconds: sudden 3000-count drop, followed by
    # gas accumulating back to the preceding peak, with slow overall drift.
    for i in range(2400):
        elapsed = i*.025
        phase = elapsed % 3.
        drop = 0 if phase < 1. else 3000 if phase < 1.3 else 3000*(3.-phase)/1.7
        raw = BASELINE + sign*(elapsed*10 - drop)
        detector.update(raw, 1.+elapsed)
        assert detector.ready
    assert detector.count == 20 and detector.recovery_count == 0


def test_bounded_recovery_does_not_require_a_quiet_band():
    detector = BubbleDetector()
    warmup(detector)
    activate(detector, BASELINE-50000)
    result = detector.update(BASELINE-50000, 2.2)
    assert result['transition'] == 'recovering'
    for i in range(1, 6):
        result = detector.update(BASELINE-50000 + i*600, 2.2+i*.1)
    assert result['transition'] == 'recovered'
    assert detector.ready and result['drop'] == 0
    assert detector.count == 1


def test_manual_reset_clears_recovery_but_preserves_count():
    detector = BubbleDetector()
    warmup(detector)
    activate(detector, BASELINE-50000)
    detector.update(BASELINE-50000, 2.2)
    assert detector.recovering
    detector.reset()
    assert detector.count == 1 and not detector.ready
    assert not detector.recovering and detector.state == 'warming_up'


def test_pressure_telemetry_marks_recovery_and_keeps_kpa_publication(monkeypatch):
    client = Mock()
    client.is_connected.return_value = True
    monkeypatch.setattr('pi.pressure.make_client', lambda _: client)
    hardware = PressureHardwareClient('both')
    for i in range(4):
        hardware.process_sample(BASELINE, i*.025)
    hardware.process_sample(BASELINE-50000, 1)
    hardware.process_sample(BASELINE-50000, 1.16)
    original_pressure = hardware.raw_to_kpa(BASELINE-50000)
    session = hardware.session
    client.reset_mock()
    result = hardware.process_sample(BASELINE-50000, 2.2, publish=False)
    assert result['transition'] == 'recovering'
    payloads = {call.args[0]: json.loads(call.args[1]) for call in client.publish.call_args_list}
    assert payloads[config.TOPIC_PRESSURE_STATUS]['ready'] is False
    assert payloads[config.TOPIC_PRESSURE_STATUS]['detector_state'] == 'recovering'
    assert payloads[config.TOPIC_BUBBLE]['bubble'] is False
    assert payloads[config.TOPIC_BUBBLE]['count'] == 1
    assert payloads[config.TOPIC_PRESSURE]['pressure'] == round(original_pressure, 4)
    assert f'{config.ROOT}/diagnostics/pressure/window_drop' in payloads
    assert config.TOPIC_BUBBLE_EVENT not in payloads
    for i in range(1, 7):
        hardware.process_sample(BASELINE-50000, 2.2+i*.1, publish=False)
    assert hardware.latest_status['ready']
    assert hardware.latest_status['detector_state'] == 'ready'
    assert hardware.raw_to_kpa(BASELINE-50000) == original_pressure
    assert hardware.detector.count == 1 and hardware.session == session


def test_reset_request_still_works_during_recovery(monkeypatch):
    client = Mock()
    client.is_connected.return_value = True
    monkeypatch.setattr('pi.pressure.make_client', lambda _: client)
    hardware = PressureHardwareClient('both')
    warmup(hardware.detector)
    activate(hardware.detector, BASELINE-50000)
    hardware.process_sample(BASELINE-50000, 2.2)
    assert hardware.detector.recovering
    hardware._on_message(client, None, SimpleNamespace(topic=config.TOPIC_BUBBLE_RESET,
        payload=b'{"action":"RELEARN"}', retain=False))
    hardware.process_sample(BASELINE-50000, 2.3)
    assert hardware.detector.count == 1
    assert not hardware.detector.recovering and hardware.detector.state == 'warming_up'


@pytest.mark.parametrize('name', ['PRESSURE_MAX_ACTIVE_SECONDS', 'PRESSURE_RECOVERY_SECONDS',
                                 'PRESSURE_RELEARN_STABLE_SECONDS', 'PRESSURE_BASELINE_RISE_ALPHA'])
@pytest.mark.parametrize('value', [True, 0, -1, float('nan'), float('inf'), 'invalid'])
def test_invalid_recovery_timing_rejected(monkeypatch, name, value):
    monkeypatch.setattr(config, name, value, raising=False)
    with pytest.raises(ValueError):
        BubbleDetector()


@pytest.mark.parametrize('state,label', [('recovering', 'relearn'), ('warming_up', 'warming')])
def test_live_pressure_visible_while_detector_unavailable(tmp_path, monkeypatch, state, label):
    import dashboard
    from monitoring.monitoring_service import MonitoringService
    from pi.oled import screen_lines
    monitor = MonitoringService(tmp_path/'readings.csv')
    monitor.connected = True
    def receive(topic, data):
        monitor.handle_message(topic, json.dumps(data).encode(), False)
    receive(config.TOPIC_PRESSURE, {'pressure': 1.2})
    receive(config.TOPIC_PRESSURE_STATUS, {'online': True, 'ready': False, 'mode': 'both',
            'calibrated': True, 'error': None, 'detector_state': state})
    receive(config.TOPIC_BUBBLE, {'bubble': False, 'count': 7, 'ready': False, 'state': state})
    assert monitor.pressure_summary()['live']
    assert not monitor.bubble_summary()['live']
    assert monitor.bubble_summary()['state'] == state
    assert monitor._last_coverage_available is False
    lines = screen_lines(monitor)
    assert 'Pressure: 1.20kPa' in lines
    assert f'Bubbles: 7 {label}' in lines
    assert not any('stale' in line for line in lines)
    monkeypatch.setattr(dashboard, 'monitor', monitor)
    pressure_text, bubble_text = dashboard.refresh_bubbles(0)
    assert 'stale' not in pressure_text and 'stale' not in bubble_text
    assert ('relearning baseline' if state == 'recovering' else 'warming up') in bubble_text
    # Readiness resumes without restarting the monitoring processes.
    receive(config.TOPIC_PRESSURE_STATUS, {'online': True, 'ready': True, 'mode': 'both',
            'calibrated': True, 'error': None, 'detector_state': 'ready'})
    receive(config.TOPIC_BUBBLE, {'bubble': False, 'count': 7, 'ready': True, 'state': 'ready'})
    assert monitor.bubble_summary()['live']
    assert monitor._last_coverage_available is True


@pytest.mark.parametrize('failure', ['retained', 'old', 'offline', 'disconnected', 'read_error'])
def test_true_pressure_failures_remain_unavailable(tmp_path, monkeypatch, failure):
    from monitoring.monitoring_service import MonitoringService
    monitor = MonitoringService(tmp_path/'readings.csv', log_enabled=False)
    monitor.connected = failure != 'disconnected'
    status = {'online': failure != 'offline', 'ready': True, 'mode': 'both', 'calibrated': True,
              'error': 'ADC timeout' if failure == 'read_error' else None}
    monitor.handle_message(config.TOPIC_PRESSURE_STATUS, json.dumps(status).encode(), failure == 'retained')
    monitor.handle_message(config.TOPIC_BUBBLE, b'{"bubble":false,"count":7,"ready":true}', False)
    if failure == 'old':
        received = monitor.messages[config.TOPIC_PRESSURE_STATUS][1]
        monkeypatch.setattr('monitoring.monitoring_service.time.monotonic',
                            lambda: received + config.PRESSURE_STALE_AFTER_S + 1)
    assert not monitor.pressure_summary()['live']
    assert not monitor.bubble_summary()['live']
    assert monitor.bubble_summary()['state'] == 'stale'
