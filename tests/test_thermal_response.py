"""Slow but fresh temperatures must not shut down a working actuator."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import config
from controller_system.biodigester_controller import BiodigesterController
from controller_system.fault_manager import FaultManager
from controller_system.heater_monitor import HeaterMonitor
from controller_system.sensor_monitor import TemperatureSensorMonitor


def test_thermal_warning_waits_for_window_and_clears_without_ack(monkeypatch):
    monkeypatch.setattr(config,'PELTIER_RESPONSE_WINDOW_S',600)
    monkeypatch.setattr(config,'PELTIER_MIN_RISE_C',.08)
    monitor=HeaterMonitor()
    faults=FaultManager()
    args=dict(commanded_on=True,relay_reported_on=True,temperature_trustworthy=True,
              safety_cutout=False,commanded_power=100,applied_power=100)
    assert not monitor.evaluate(now=0,temperature_c=34,**args)['HEATER_NO_THERMAL_RESPONSE']
    assert not monitor.evaluate(now=599,temperature_c=34.01,**args)['HEATER_NO_THERMAL_RESPONSE']
    conditions=monitor.evaluate(now=600,temperature_c=34.01,**args)
    assert conditions['HEATER_NO_THERMAL_RESPONSE']
    faults.update_condition('HEATER_NO_THERMAL_RESPONSE',True,600)
    warning=faults.active_faults()[0]
    assert warning['severity']=='WARNING' and not warning['latching']
    assert not faults.heating_inhibited() and not faults.safety_trip_required()
    conditions=monitor.evaluate(now=650,temperature_c=34.1,**args)
    faults.update_condition('HEATER_NO_THERMAL_RESPONSE',conditions['HEATER_NO_THERMAL_RESPONSE'],650)
    assert not faults.active_codes()
    assert not monitor.evaluate(now=700,temperature_c=34.1,**args)['HEATER_NO_THERMAL_RESPONSE']
    assert monitor.evaluate(now=1250,temperature_c=34.1,**args)['HEATER_NO_THERMAL_RESPONSE']


def test_idle_history_cannot_immediately_raise_flat_temperature_warning():
    monitor=TemperatureSensorMonitor(stuck_window_s=10)
    for now in range(21):
        monitor.add_reading(34,now)
        assert not monitor.evaluate(now,heating_expected=False).conditions['TEMP_SENSOR_STUCK']
    assert not monitor.evaluate(20,heating_expected=True).conditions['TEMP_SENSOR_STUCK']
    for now in range(21,30):
        monitor.add_reading(34,now)
        assert not monitor.evaluate(now,heating_expected=True).conditions['TEMP_SENSOR_STUCK']
    monitor.add_reading(34,30)
    health=monitor.evaluate(30,heating_expected=True)
    assert health.conditions['TEMP_SENSOR_STUCK'] and health.trustworthy and health.value==34
    monitor.evaluate(31,heating_expected=False)
    monitor.add_reading(34,32)
    assert not monitor.evaluate(32,heating_expected=True).conditions['TEMP_SENSOR_STUCK']


def controller_with_clock():
    clock=[0]
    client=Mock()
    client.is_connected.return_value=True
    controller=BiodigesterController(client,clock=lambda:clock[0])
    return clock,client,controller


def deliver(client,controller,topic,payload):
    controller._on_message(client,None,SimpleNamespace(topic=topic,payload=json.dumps(payload).encode(),retain=False))


def heat_cycle(clock,client,controller,now,value=34,power=None):
    clock[0]=now
    power=(30 if controller.temperature_control.heating else 0) if power is None else power
    deliver(client,controller,config.TOPIC_TEMPERATURE,{'temperature':value})
    deliver(client,controller,config.TOPIC_PELTIER_STATE,{'applied_power':power,'safety_latched':False})
    return controller.step()


def test_fresh_constant_temperature_keeps_heating_beyond_both_warning_windows():
    clock,client,controller=controller_with_clock()
    for now in range(0,1203,2):
        status=heat_cycle(clock,client,controller,now)
        assert status['heating'] and status['mode']=='HEATING'
    codes=controller.faults.active_codes()
    assert {'TEMP_SENSOR_STUCK','HEATER_NO_THERMAL_RESPONSE'} <= codes
    assert not controller.faults.heating_inhibited()
    assert all(f['severity']=='WARNING' for f in status['faults'])
    safety=[c for c in client.publish.call_args_list if c.args[0]==config.TOPIC_PELTIER_SAFETY_CMD]
    assert not safety
    for now in (1204,1206,1208):
        status=heat_cycle(clock,client,controller,now,value=34.2)
    assert status['heating']
    assert not ({'TEMP_SENSOR_STUCK','HEATER_NO_THERMAL_RESPONSE'} & controller.faults.active_codes())


def test_old_heating_setting_uses_cap_for_commands_and_feedback(monkeypatch):
    monkeypatch.setattr(config, 'PELTIER_HEAT_POWER', 100, raising=False)
    monkeypatch.setattr(config, 'PELTIER_MAX_POWER', 30)
    clock,client,controller=controller_with_clock()
    for now in range(0,30,2):
        status=heat_cycle(clock,client,controller,now)
        assert status['heating']
        commands=[json.loads(c.args[1]) for c in client.publish.call_args_list
                  if c.args[0]==config.TOPIC_PELTIER_CMD]
        assert commands[-1]['power']==30
    assert 'PELTIER_POWER_MISMATCH' not in controller.faults.active_codes()


@pytest.mark.parametrize('limit', [5, 10, 30, 65, 100])
def test_one_config_setting_controls_actual_heating_command(monkeypatch, limit):
    monkeypatch.setattr(config, 'PELTIER_MAX_POWER', limit)
    # A leftover old setting must not restrict or override the new one.
    monkeypatch.setattr(config, 'PELTIER_HEAT_POWER', 5, raising=False)
    clock,client,controller=controller_with_clock()
    for now in range(0,30,2):
        previous=limit if controller.temperature_control.heating else 0
        status=heat_cycle(clock,client,controller,now,power=previous)
        assert status['heating'] and status['commanded_power']==limit
    assert 'PELTIER_POWER_MISMATCH' not in controller.faults.active_codes()


@pytest.mark.parametrize('limit', [0, 0.5])
def test_zero_or_sub_deadband_cap_disables_heat_without_false_faults(monkeypatch, limit):
    monkeypatch.setattr(config, 'PELTIER_MAX_POWER', limit)
    clock,client,controller=controller_with_clock()
    for now in range(0,30,2):
        status=heat_cycle(clock,client,controller,now,power=0)
        assert not status['heating'] and status['commanded_power']==0
    assert not controller.faults.heating_inhibited()


@pytest.mark.parametrize('reported', [100, -100])
def test_over_limit_driver_feedback_trips_on_first_cycle(reported):
    clock,client,controller=controller_with_clock()
    status=heat_cycle(clock,client,controller,0,power=reported)
    assert not status['heating'] and status['commanded_power']==0
    assert 'PELTIER_POWER_LIMIT_EXCEEDED' in controller.faults.active_codes()
    safety=[json.loads(c.args[1]) for c in client.publish.call_args_list
            if c.args[0]==config.TOPIC_PELTIER_SAFETY_CMD]
    assert safety[-1]=={'action':'TRIP'}


@pytest.mark.parametrize('value,code',[(float('nan'),'TEMP_SENSOR_OUT_OF_RANGE'),
                                     (True,'TEMP_SENSOR_OUT_OF_RANGE'),
                                     (-127,'TEMP_SENSOR_OUT_OF_RANGE'),
                                     (40.1,'HIGH_TEMPERATURE'),
                                     (43,'HIGH_HIGH_TEMPERATURE')])
def test_real_sensor_and_temperature_faults_still_force_zero(value,code):
    clock,client,controller=controller_with_clock()
    assert heat_cycle(clock,client,controller,0)['heating']
    assert not heat_cycle(clock,client,controller,2,value)['heating']
    assert code in controller.faults.active_codes()
    commands=[json.loads(c.args[1]) for c in client.publish.call_args_list if c.args[0]==config.TOPIC_PELTIER_CMD]
    assert commands[-1]=={'power':0}
    if code=='HIGH_HIGH_TEMPERATURE':
        assert controller.faults.safety_trip_required()


def test_sensor_staleness_and_actuator_feedback_staleness_still_stop_heat():
    clock,client,controller=controller_with_clock()
    assert heat_cycle(clock,client,controller,0)['heating']
    clock[0]=11
    assert not controller.step()['heating']
    assert {'TEMP_SENSOR_STALE','PELTIER_FEEDBACK_STALE'} <= controller.faults.active_codes()
