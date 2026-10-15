"""Hardware contracts with controlled clocks and GPIO, without energising pins."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import config
from controller_system.biodigester_controller import BiodigesterController
from monitoring.monitoring_service import MonitoringService
from pi.bubble_detection import BubbleDetector
from pi.peltier import PeltierHardwareClient
from pi.pressure import PressureHardwareClient
from pi.temp import TemperatureHardwareClient
from pi.oled import draw_screen, screen_lines, open_device


def message(topic, data, retain=False):
    return SimpleNamespace(topic=topic, payload=json.dumps(data).encode(), retain=retain)


@pytest.fixture
def client(monkeypatch):
    client = Mock()
    client.is_connected.return_value = True
    for module in ('pi.peltier', 'pi.pressure', 'pi.temp'):
        monkeypatch.setattr(f'{module}.make_client', lambda _: client)
    return client


@pytest.fixture
def peltier(client, monkeypatch):
    gpio = Mock()
    gpio.gpiochip_open.return_value = 7
    monkeypatch.setattr(PeltierHardwareClient, '_lgpio', lambda _: gpio)
    hardware = PeltierHardwareClient(clock=lambda: 0)
    hardware.setup_gpio()
    return hardware, gpio


def test_bridge_preserves_pins_and_breaks_before_reversing(peltier, monkeypatch):
    hw, gpio = peltier
    assert [c.args[1] for c in gpio.gpio_claim_output.call_args_list] == [18, 19]
    hw.set_power(60)
    gpio.reset_mock()
    delays = []
    monkeypatch.setattr('pi.peltier.time.sleep', lambda delay: delays.append(delay))
    hw.set_power(-40)
    calls = [c.args for c in gpio.tx_pwm.call_args_list]
    assert calls[:2] == [(7, 18, 1000, 0), (7, 19, 1000, 0)]
    assert calls[-1] == (7, 19, 1000, 30)
    assert delays == [config.PELTIER_DIRECTION_CHANGE_DELAY_S]
    hw.set_power(0)
    assert hw.applied_power == 0
    hw.cleanup_gpio()
    gpio.gpiochip_close.assert_called_once_with(7)


@pytest.mark.parametrize('value', [True, None, 'bad', float('nan'), float('inf'), 101, -101])
def test_invalid_power_never_drives_output(peltier, value):
    hw, _ = peltier
    hw._on_message(hw.client, None, message(config.TOPIC_PELTIER_CMD, {'power': value}))
    assert hw.applied_power == 0
    assert hw.last_command_time is None


@pytest.mark.parametrize('requested,applied', [(100, 30), (-100, -30), (20, 20), (-20, -20), (0, 0)])
def test_manual_commands_obey_both_direction_limits(peltier, requested, applied):
    hw, gpio = peltier
    hw._on_message(hw.client, None, message(config.TOPIC_PELTIER_CMD, {'power': requested}))
    assert hw.requested_power == requested
    assert hw.applied_power == applied
    assert all(call.args[3] <= 30 for call in gpio.tx_pwm.call_args_list)


def test_configurable_limit_and_old_config_fallback(peltier, monkeypatch):
    hw, _ = peltier
    monkeypatch.setattr(config, 'PELTIER_MAX_POWER', 15)
    hw.set_power(-100)
    assert hw.applied_power == -15
    monkeypatch.delattr(config, 'PELTIER_MAX_POWER')
    hw.set_power(100)
    assert hw.applied_power == 30


@pytest.mark.parametrize('limit', [True, -1, 101, float('nan'), float('inf'), 'bad'])
def test_invalid_limit_rejected_before_pwm_change(peltier, monkeypatch, limit):
    hw, _ = peltier
    hw.set_power(20)
    monkeypatch.setattr(config, 'PELTIER_MAX_POWER', limit)
    # Invalid configuration is rejected before changing PWM.
    with pytest.raises((ValueError, TypeError)):
        hw.set_power(100)


def test_runtime_limit_error_turns_previous_output_off(peltier, monkeypatch):
    hw, _ = peltier
    hw.set_power(20)
    monkeypatch.setattr(config, 'PELTIER_MAX_POWER', 'bad')
    # State telemetry itself validates the config, so mock publication here.
    monkeypatch.setattr(hw, 'publish_state', lambda: None)
    hw._on_message(hw.client, None, message(config.TOPIC_PELTIER_CMD, {'power': 100}))
    assert hw.applied_power == 0 and hw.safety_latched


def test_saved_commands_trip_reset_and_watchdog(peltier):
    hw, _ = peltier
    hw._on_message(hw.client, None, message(config.TOPIC_PELTIER_CMD, {'power': 100}, True))
    assert hw.applied_power == 0
    hw._on_message(hw.client, None, message(config.TOPIC_PELTIER_CMD, {'power': 100}))
    assert hw.applied_power == 30
    hw.clock = lambda: 11
    assert hw.watchdog_step() and hw.safety_latched and hw.applied_power == 0
    hw._on_message(hw.client, None, message(config.TOPIC_PELTIER_CMD, {'power': 100}))
    assert hw.applied_power == 0
    hw._on_message(hw.client, None, message(config.TOPIC_PELTIER_SAFETY_CMD, {'action': 'RESET'}, True))
    assert hw.safety_latched
    hw._on_message(hw.client, None, message(config.TOPIC_PELTIER_SAFETY_CMD, {'action': 'RESET'}))
    assert not hw.safety_latched and hw.applied_power == 0
    hw._on_message(hw.client, None, message(config.TOPIC_PELTIER_CMD, {'power': 20}))
    assert hw.applied_power == 20
    hw._on_disconnect()
    assert hw.safety_latched and hw.applied_power == 0


def test_failed_bridge_start_closes_gpio(peltier, monkeypatch):
    hw, gpio = peltier
    gpio.gpio_claim_output.side_effect = RuntimeError('claim failed')
    with pytest.raises(RuntimeError):
        hw.run()
    assert hw.gpio is None


def test_pressure_read_signed_bits_and_27_pulses(client, monkeypatch):
    hw = PressureHardwareClient()
    gpio = Mock()
    gpio.gpiochip_open.return_value = 5
    monkeypatch.setattr(hw, '_lgpio', lambda: gpio)
    hw.setup_gpio()
    assert [c.args[1] for c in gpio.gpio_claim_output.call_args_list] == [23]
    assert gpio.gpio_claim_input.call_args.args[1] == 24
    gpio.gpio_read.side_effect = [0] + [1] * 24
    gpio.reset_mock(side_effect=False)
    assert hw.read_raw() == -1
    highs = [c for c in gpio.gpio_write.call_args_list if c.args[-1] == 1]
    assert len(highs) == 27
    hw.cleanup_gpio()
    gpio.gpiochip_close.assert_called_once_with(5)


@pytest.mark.parametrize('cal_raw', [2000000, 700000])
def test_pressure_calibration_both_slopes(client, monkeypatch, cal_raw):
    monkeypatch.setattr(config, 'PRESSURE_ZERO_RAW', 1000000)
    monkeypatch.setattr(config, 'PRESSURE_CAL_RAW', cal_raw)
    monkeypatch.setattr(config, 'PRESSURE_CAL_KPA', 2.5)
    hw = PressureHardwareClient()
    assert hw.calibration_enabled()
    assert hw.raw_to_kpa(1000000) == 0
    assert hw.raw_to_kpa(cal_raw) == 2.5
    assert hw.raw_to_kpa((1000000+cal_raw)/2) == 1.25
    monkeypatch.setattr(config, 'PRESSURE_CAL_RAW', 1000000)
    assert not hw.calibration_enabled()
    with pytest.raises(ValueError):
        hw.raw_to_kpa(1000000)


@pytest.mark.parametrize('direction', [True, False])
def test_bubble_noise_hold_recovery_and_second_event(monkeypatch, direction):
    monkeypatch.setattr(config, 'PRESSURE_WARMUP_SAMPLES', 4)
    monkeypatch.setattr(config, 'PRESSURE_DROP_WHEN_RAW_DECREASES', direction)
    d = BubbleDetector()
    for i in range(4):
        d.update(1000000 + i % 2, i * .025)
    drop_raw = 980000 if direction else 1020000
    assert d.update(drop_raw, 1)['event'] is None
    assert d.update(1000000, 1.05)['event'] is None  # short spike rejected
    d.update(drop_raw, 2)
    assert d.update(drop_raw, 2.16)['event']['count'] == 1
    assert d.update(drop_raw, 3)['event'] is None  # held drop counted once
    d.update(1000000, 4)
    d.update(drop_raw, 4.1)  # recovery interrupted
    assert d.active
    d.update(1000000, 5)
    d.update(1000000, 5.26)
    assert not d.active
    d.update(drop_raw, 6)
    assert d.update(drop_raw, 6.16)['event']['count'] == 2
    d.reset()
    assert d.baseline is None and d.candidate_start is None


@pytest.mark.parametrize('mode,pressure,bubble', [('pressure',True,False),('bubble',False,True),('both',True,True)])
def test_pressure_mode_selects_publications(client, monkeypatch, mode, pressure, bubble):
    monkeypatch.setattr(config, 'PRESSURE_CAL_RAW', 2000000)
    monkeypatch.setattr(config, 'PRESSURE_CAL_KPA', 2)
    hw = PressureHardwareClient(mode)
    hw.process_sample(1500000, 0)
    topics = {c.args[0] for c in client.publish.call_args_list}
    assert (config.TOPIC_PRESSURE in topics) == pressure
    assert (config.TOPIC_BUBBLE in topics) == bubble


def test_temperature_discovery_and_reading(client, tmp_path, monkeypatch):
    folder = tmp_path / '28-sensor'
    folder.mkdir()
    (folder/'temperature').write_text('36562')
    monkeypatch.setattr(config, 'TEMP_SENSOR_GLOB', str(tmp_path/'28-*'))
    hw = TemperatureHardwareClient()
    assert hw.read_temperature() == 36.562
    hw.publish_temperature(hw.read_temperature())
    assert json.loads(client.publish.call_args.args[1]) == {'temperature':36.562}
    (folder/'temperature').write_text('85000')
    with pytest.raises(ValueError): hw.read_temperature()
    (folder/'temperature').unlink()
    with pytest.raises(FileNotFoundError): hw.read_temperature()


def test_controller_needs_live_feedback_and_live_sensor(client):
    clock = [0]
    c = BiodigesterController(client, clock=lambda:clock[0])
    c._on_message(client,None,message(config.TOPIC_TEMPERATURE, {'temperature':34},True))
    assert not c.step()['heating']
    c._on_message(client,None,message(config.TOPIC_TEMPERATURE, {'temperature':34}))
    assert not c.step()['heating']
    c._on_message(client,None,message(config.TOPIC_PELTIER_STATE, {'applied_power':0,'safety_latched':False}))
    assert c.step()['heating']
    clock[0] = 6
    assert not c.step()['heating']
    assert 'PELTIER_FEEDBACK_STALE' in c.faults.active_codes()
    commands = [json.loads(call.args[1]) for call in client.publish.call_args_list if call.args[0] == config.TOPIC_PELTIER_CMD]
    assert commands[-1] == {'power':0}


def test_bubble_ingestion_deduplicates_and_expires(tmp_path, monkeypatch):
    clock = [0]
    monkeypatch.setattr('monitoring.monitoring_service.time.monotonic', lambda:clock[0])
    m = MonitoringService(tmp_path/'r.csv',tmp_path/'f.csv',tmp_path/'b.csv')
    m.connected = True
    def receive(topic, data, retained=False):m.handle_message(topic,json.dumps(data).encode(),retained)
    receive(config.TOPIC_PRESSURE_STATUS, {'online':True,'ready':True,'mode':'both','calibrated':False})
    receive(config.TOPIC_BUBBLE, {'bubble':True,'count':1,'session':'a'})
    event = {'event':'bubble','session':'a','count':1}
    receive(config.TOPIC_BUBBLE_EVENT,event,True)
    assert m.bubble_summary()['rate'] == 0
    receive(config.TOPIC_BUBBLE_EVENT,event)
    receive(config.TOPIC_BUBBLE_EVENT,event)
    assert m.bubble_summary()['rate'] == 1
    assert m.bubble_summary()['live']
    assert len((tmp_path/'b.csv').read_text().splitlines()) == 2
    clock[0] = 61
    assert m.bubble_summary()['rate'] == 0 and not m.bubble_summary()['live']


def test_oled_renders_128_64_and_stale_values(tmp_path):
    m = MonitoringService(log_enabled=False)
    lines = screen_lines(m)
    assert 'Temperature: --' in lines and 'Pressure: --' in lines
    device = SimpleNamespace(size=(128,64),width=128,display=Mock())
    image = draw_screen(device, lines)
    assert image.size == (128,64) and image.mode == '1'
    assert image.getbbox() is not None
    device.display.assert_called_once()


def test_oled_actual_sh1106_driver_uses_nonconflicting_spi_pins(monkeypatch):
    import sys
    serial = pytest.importorskip('luma.core.interface.serial')
    pytest.importorskip('luma.oled.device')
    gpio = Mock()
    gpio.gpiochip_open.return_value = 4
    bus = Mock()
    monkeypatch.setitem(sys.modules, 'lgpio', gpio)
    monkeypatch.setattr(serial.spi, "__spidev__", lambda _: bus)
    device, outputs = open_device()
    try:
        assert type(device).__name__ == 'sh1106'
        assert device.size == (128, 64)
        bus.open.assert_called_once_with(0, 0)
        assert {c.args[1] for c in gpio.gpio_claim_output.call_args_list} == {25,27}
        draw_screen(device, ['Digester IDLE', 'Temperature: 36.5C', 'Pressure: 2.3kPa',
                             'Bubbles: 10 3/m', 'Peltier: 0%', 'MQTT connected'])
        assert bus.writebytes.call_count > 0
    finally:
        device.cleanup()
        outputs.close()
    assert gpio.gpiochip_close.call_args.args == (4,)


def test_pressure_clock_does_not_reset_based_on_python_call_latency(client, monkeypatch):
    hw = PressureHardwareClient()
    hw.gpio = 1
    gpio = Mock()
    gpio.gpio_read.return_value = 1
    monkeypatch.setattr(hw, '_lgpio', lambda: gpio)
    # Slow Python clock/call measurements must not trigger invented ADC faults.
    timer = Mock(side_effect=AssertionError("Do not infer pulse width from Python timing"))
    sleeper = Mock(side_effect=AssertionError("Do not sleep during a clock pulse"))
    monkeypatch.setattr('pi.pressure.time.perf_counter_ns', timer)
    monkeypatch.setattr('pi.pressure.time.sleep', sleeper)
    assert hw._pulse(read_bit=True) == 1
    assert [c.args[-1] for c in gpio.gpio_write.call_args_list] == [1, 0]
    gpio.gpio_read.assert_called_once_with(1, config.PRESSURE_OUT_PIN)
    timer.assert_not_called()
    sleeper.assert_not_called()


def test_pressure_clock_is_lowered_if_high_write_raises(client, monkeypatch):
    hw = PressureHardwareClient()
    hw.gpio = 1
    gpio = Mock()
    gpio.gpio_write.side_effect = [OSError("write failed"), None]
    monkeypatch.setattr(hw, '_lgpio', lambda: gpio)
    with pytest.raises(OSError, match="write failed"):
        hw._pulse(read_bit=True)
    assert [c.args[-1] for c in gpio.gpio_write.call_args_list] == [1, 0]
    gpio.gpio_read.assert_not_called()


def test_signed_power_mismatch_trips_safety_after_grace():
    from controller_system.heater_monitor import HeaterMonitor
    monitor = HeaterMonitor()
    args = dict(commanded_on=True, relay_reported_on=True, temperature_c=34,
                temperature_trustworthy=True, safety_cutout=False,
                commanded_power=100, applied_power=-100)
    assert not monitor.evaluate(now=0, **args)['PELTIER_POWER_MISMATCH']
    assert monitor.evaluate(now=5, **args)['PELTIER_POWER_MISMATCH']
