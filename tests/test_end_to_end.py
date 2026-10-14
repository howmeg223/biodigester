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
    advance(lambda: m.controller_status().get('mode')=='HEATING' and s.heater.power==100)
    s.fault_injector.apply('temp_sensor_out_of_range')
    advance(lambda: s.heater.power==0 and 'TEMP_SENSOR_OUT_OF_RANGE' in c.faults.active_codes())
    assert not c.temperature_control.heating
    s.fault_injector.apply('none')
    c.temperature_control._last_switch = None
    advance(lambda:s.heater.power==100)
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
    advance(lambda:s.heater.power==100)
    # Emulate command silence with a controlled actuator clock.
    s.heater.clock=lambda:s.heater.last_command_time+config.PELTIER_COMMAND_TIMEOUT_S+1
    assert s.heater.watchdog()
    assert s.heater.delivered_power_w==0
    s.publish_readings(14)
    advance(lambda:'SAFETY_CUTOUT_TRIPPED' in c.faults.active_codes())
    assert not c.temperature_control.heating
