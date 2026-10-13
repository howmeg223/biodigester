"""pH checks without hardware or an MQTT broker."""
import csv
import json
from unittest.mock import Mock
import pytest
import config
from monitoring.monitoring_service import MonitoringService
from monitoring.ph_monitor import classify, validate_limits
from simulation.simulator_service import SimulatorService

@pytest.mark.parametrize('low,high', [(None,6),(4,None),(6,4),(4,4),(-1,6),(4,15),(float('nan'),6),(True,6)])
def test_invalid_limits(low, high):
    assert validate_limits(low, high)

def test_boundaries():
    assert validate_limits(0,14) is None
    assert all(classify(v,4.2,6)[0]=='Within limits' for v in (4.2,6))
    assert classify(4.1,4.2,6)[0]=='Low pH'
    assert classify(6.1,4.2,6)[0]=='High pH'

def test_sampling(monkeypatch):
    monkeypatch.setattr(config,'SIM_PH_ENABLED',True)
    client=Mock()
    sim=SimulatorService(client,speed=600)
    for now in (0,1,9.99,10,20,30,40,50):
        sim.publish_ph_if_due(now)
    calls=client.publish.call_args_list
    assert len(calls)==6
    samples=[json.loads(c.args[1]) for c in calls]
    assert all(c.args[0]==config.TOPIC_PH for c in calls)
    assert all(p['simulated'] for p in samples)
    assert all(4.2<=p['ph']<=6 for p in samples[:4])
    assert samples[4]['ph']<4.2 and samples[5]['ph']>6
    sim.publish_ph_if_due(500)
    sim.publish_ph_if_due(500)
    assert client.publish.call_count==7
    monkeypatch.setattr(config,'SIM_PH_ENABLED',False)
    sim.publish_ph_if_due(1000)
    assert client.publish.call_count==7

def test_ingestion(tmp_path):
    monitor=MonitoringService(tmp_path/'readings.csv',tmp_path/'faults.csv')
    for payload in ({'ph':-1},{'ph':15},{'ph':True},{'ph':float('nan')},{'ph':5,'valid':False}):
        monitor.handle_message(config.TOPIC_PH,json.dumps(payload).encode())
    assert not monitor.latest
    monitor.handle_message(config.TOPIC_PH,b'{"ph":5}',retained=True)
    assert not monitor.log_file.exists()
    monitor.handle_message(config.TOPIC_PH,b'{"ph":3.9,"simulated":true}')
    assert monitor.ph_simulated
    with monitor.log_file.open() as f:
        assert len(list(csv.DictReader(f)))==1

def test_dashboard(tmp_path,monkeypatch):
    import dashboard
    from dash import no_update
    monitor=MonitoringService(tmp_path/'readings.csv',tmp_path/'faults.csv')
    monitor.connected=True
    monkeypatch.setattr(dashboard,'monitor',monitor)
    monkeypatch.setattr(dashboard,'lock',monitor.lock)
    limits={'low':4.2,'high':6}
    notification,figure,_=dashboard.refresh_ph(0,limits,'All data')
    assert 'Waiting' in notification.children
    assert len(figure.layout.shapes)==2
    for value,label in [(3.9,'Low pH'),(6.4,'High pH'),(4.2,'Within limits')]:
        monitor.handle_message(config.TOPIC_PH,json.dumps({'ph':value,'simulated':True}).encode())
        notification,figure,style=dashboard.refresh_ph(0,limits,'All data')
        assert label in notification.children and 'Simulated pH' in notification.children
        assert style['color']==classify(value,4.2,6)[1]
    assert figure.data[0].mode=='markers' and len(figure.data[0].y)==3
    assert [s.y0 for s in figure.layout.shapes]==[4.2,6]
    assert dashboard.history_fig('ph',None,limits).data[0].mode=='markers'
    report=dashboard.report_fig(['temperature','ph'],None,limits)
    assert report.data[1].mode=='markers' and len(report.layout.shapes)==2
    monitor.connected=False
    assert 'No recent pH reading' in dashboard.refresh_ph(0,limits,'All data')[0].children
    monitor.connected=True
    old=monitor.latest['ph']
    monitor.latest['ph']=(*old[:3],old[3]-31)
    assert 'No recent pH reading' in dashboard.refresh_ph(0,limits,'All data')[0].children
    monitor.handle_message(config.TOPIC_PH,b'{"ph":5}',retained=True)
    assert 'measurement time unknown' in dashboard.refresh_ph(0,limits,'All data')[0].children
    assert dashboard.save_ph_limits(1,6,4)[0] is no_update
    assert dashboard.save_ph_limits(1,4,7)[0]=={'low':4,'high':7}
    assert dashboard.restore_ph_limits({'low':9,'high':1})==(4.2,6)
    monitor.client=Mock()
    dashboard.save_ph_limits(1,4,7)
    monitor.client.publish.assert_not_called()
