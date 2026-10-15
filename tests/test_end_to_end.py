"""Real MQTT integration on a private ephemeral broker; never uses Pi broker/topics.

Install requirements-test.txt to enable these tests. No GPIO is accessed.
"""
import asyncio
import socket
import threading
import time

import pytest
import config
from controller_system.biodigester_controller import BiodigesterController
from monitoring.monitoring_service import MonitoringService
from mqtt_common import make_client
from simulation.simulator_service import SimulatorService


@pytest.fixture
def system(tmp_path, monkeypatch):
    Broker = pytest.importorskip('amqtt.broker').Broker
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    ready, stop = threading.Event(), threading.Event()
    errors = []
    async def serve():
        broker = Broker({'listeners':{'default':{'type':'tcp','bind':f'127.0.0.1:{port}'}},
                         'plugins':{'amqtt.plugins.authentication.AnonymousAuthPlugin':{'allow_anonymous':True}}})
        try:
            await broker.start()
            ready.set()
            while not stop.is_set(): await asyncio.sleep(.02)
        finally:
            await broker.shutdown()
    def run():
        try: asyncio.run(serve())
        except asyncio.CancelledError: pass  # broker cancels pending QoS tasks during shutdown
        except Exception as exc: errors.append(exc); ready.set()
    thread = threading.Thread(target=run,daemon=True)
    thread.start()
    assert ready.wait(5) and not errors, errors
    monkeypatch.setattr(config,'BROKER_HOST','127.0.0.1')
    monkeypatch.setattr(config,'BROKER_PORT',port)
    clients=[]
    try:
        cclient, sclient = make_client('test-controller'), make_client('test-simulator')
        clients.extend([cclient,sclient])
        controller = BiodigesterController(cclient)
        simulator = SimulatorService(sclient,start_c=34,speed=1)
        monitor = MonitoringService(tmp_path/'r.csv',tmp_path/'f.csv',tmp_path/'b.csv')
        for client in clients:
            client.connect('127.0.0.1',port,keepalive=5)
            client.loop_start()
        monitor.start()
        def advance(predicate, timeout=5):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                simulator.publish_readings(14)
                controller.step()
                if predicate(): return
                time.sleep(.05)
            pytest.fail(f'MQTT condition timed out: {controller.faults.active_codes()}')
        advance(lambda: monitor.connected and bool(monitor.latest))
        yield controller,simulator,monitor,advance
    finally:
        if 'controller' in locals(): controller.shutdown()
        if 'monitor' in locals(): monitor.stop()
        for client in clients:
            client.disconnect();client.loop_stop()
        stop.set();thread.join(5)


def test_real_mqtt_heating_bad_sensor_safety_ack_and_watchdog(system):
    c,s,m,advance = system
    advance(lambda: m.controller_status().get('mode')=='HEATING' and s.heater.power==30)
    assert 'sent to MQTT' in m.log_manual_ph(5.25)
    advance(lambda:m.latest.get('ph', (None,))[0]==5.25)
    assert 'manual' in m.log_file.read_text()
    s.fault_injector.apply('temp_sensor_out_of_range')
    advance(lambda: s.heater.power==0 and 'TEMP_SENSOR_OUT_OF_RANGE' in c.faults.active_codes())
    assert not c.temperature_control.heating

    s.fault_injector.apply('none')
    c.temperature_control._last_switch = None
    advance(lambda:s.heater.power==30)
    s.model.temp_c=43
    advance(lambda:s.heater.safety_cutout and 'HIGH_HIGH_TEMPERATURE' in c.faults.active_codes())
    assert s.heater.delivered_power_w==0
    # Cooling below the limit still requires acknowledgement.
    s.model.temp_c=36
    advance(lambda:not c.faults._states['HIGH_HIGH_TEMPERATURE'].condition_active)
    assert s.heater.safety_cutout
    m.acknowledge_faults()
    advance(lambda:not s.heater.safety_cutout)
    c.temperature_control._last_switch=None
    s.model.temp_c=34
    advance(lambda:s.heater.power==30)
    # Emulate command silence with a controlled actuator clock.
    s.heater.clock=lambda:s.heater.last_command_time+config.PELTIER_COMMAND_TIMEOUT_S+1
    assert s.heater.watchdog()
    assert s.heater.delivered_power_w==0
    s.publish_readings(14)
    advance(lambda:'SAFETY_CUTOUT_TRIPPED' in c.faults.active_codes())
    assert not c.temperature_control.heating


def test_real_mqtt_actual_driver_caps_automatic_and_manual_power(system, monkeypatch):
    """Exercise Pi driver callbacks and PWM arguments, rather than the simulator."""
    from unittest.mock import Mock
    from pi.peltier import PeltierHardwareClient
    import json

    c,s,m,_ = system
    s.client.disconnect()
    s.client.loop_stop()
    monkeypatch.setattr(config, 'PELTIER_HEAT_POWER', 100, raising=False)
    gpio = Mock()
    gpio.gpiochip_open.return_value = 7
    driver_client = make_client('test-real-pi-driver')
    monkeypatch.setattr('pi.peltier.make_client', lambda _: driver_client)
    monkeypatch.setattr(PeltierHardwareClient, '_lgpio', lambda _: gpio)
    hw = PeltierHardwareClient()
    hw.setup_gpio()
    try:
        driver_client.connect(config.BROKER_HOST, config.BROKER_PORT)
        driver_client.loop_start()
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            c.client.publish(config.TOPIC_TEMPERATURE, json.dumps({'temperature':34}))
            c.step()
            hw.publish_state()
            if hw.applied_power==30 and m.controller_status().get('commanded_power')==30:
                break
            time.sleep(.05)
        else:
            pytest.fail(f'Automatic PWM did not reach 30: {hw.state_payload()}')
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            hw.publish_state()
            _, messages, _ = m.snapshot()
            state = messages.get(config.TOPIC_PELTIER_STATE)
            if state and not state[2] and state[0].get('driver_file') == hw.state_payload()['driver_file']:
                assert state[0]['applied_power'] == 30
                assert state[0]['max_power'] == 30
                break
            time.sleep(.05)
        else:
            pytest.fail('Dashboard monitor did not receive live Pi driver feedback')
        assert 'PELTIER_POWER_MISMATCH' not in c.faults.active_codes()
        # Stop stepping the automatic controller; now exercise manual cooling.
        for command in (-100, 100, -20, 0):
            expected = max(-30, min(30, command))
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                c.client.publish(config.TOPIC_PELTIER_CMD,json.dumps({'power':command}))
                if hw.applied_power==expected:
                    break
                time.sleep(.05)
            assert hw.applied_power==expected
        assert all(call.args[3]<=30 for call in gpio.tx_pwm.call_args_list)
    finally:
        hw.cleanup_gpio()
        driver_client.disconnect()
        driver_client.loop_stop()
