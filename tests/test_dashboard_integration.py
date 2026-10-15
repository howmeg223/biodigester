"""Exercise Dash's actual HTTP callback transport and archived-data failure paths."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import config
from monitoring.monitoring_service import MonitoringService


@pytest.fixture
def page(tmp_path, monkeypatch):
    import dashboard
    monitor = MonitoringService(tmp_path/'readings.csv', tmp_path/'faults.csv', tmp_path/'bubbles.csv')
    monkeypatch.setattr(dashboard, 'monitor', monitor)
    monkeypatch.setattr(dashboard, 'lock', monitor.lock)
    return dashboard, monitor


def callback_request(page, key, overrides=None, changed=None):
    """Build the same inputs/expanded wildcard outputs sent by Dash's browser."""
    components = []
    def walk(component):
        if isinstance(component, (list, tuple)):
            for child in component: walk(child)
        elif hasattr(component, 'to_plotly_json'):
            props=component.to_plotly_json()['props']
            if 'id' in props: components.append(props)
            walk(props.get('children'))
    walk(page.app.layout)
    def matches(pattern, actual):
        if isinstance(pattern, dict):
            return isinstance(actual, dict) and pattern.keys()==actual.keys() and all(
                value==actual[name] or isinstance(value,list) or str(value)=='ALL'
                for name,value in pattern.items())
        return pattern==actual
    def identifier(value):
        return json.loads(value) if isinstance(value,str) and value.startswith('{') else value
    def wildcard(ident):
        return isinstance(ident, dict) and any(isinstance(v, list) or str(v)=='ALL' for v in ident.values())
    def input_value(entry):
        entry=dict(entry)
        ident=identifier(entry['id'])
        values=[p.get(entry['property']) for p in components if matches(ident,p['id'])]
        entry['value'] = values if wildcard(ident) else (values[0] if values else None)
        if overrides and entry['id'] in overrides: entry['value']=overrides[entry['id']]
        return entry
    def output_value(output):
        ident=output.component_id
        if wildcard(ident):
            return [{'id':p['id'],'property':output.component_property}
                    for p in components if matches(ident,p['id'])]
        return {'id':ident,'property':output.component_property}
    spec=page.app.callback_map[key]
    outputs=spec['output']
    outputs=[output_value(o) for o in outputs] if isinstance(outputs,list) else output_value(outputs)
    return page.app.server.test_client().post('/_dash-update-component', json={
        'output':key, 'outputs':outputs,
        'inputs':[input_value(e) for e in spec['inputs']],
        'state':[input_value(e) for e in spec['state']], 'changedPropIds':changed or [],
    })


@pytest.mark.parametrize('archive', ['empty', 'live', 'legacy', 'bad_schema'])
def test_all_dashboard_callbacks_over_http(page, archive):
    dashboard, monitor=page
    if archive=='live':
        monitor.connected=True
        for topic,payload in (
            (config.TOPIC_TEMPERATURE, {'temperature':35.125}),
            (config.TOPIC_PRESSURE, {'pressure':1.25}),
            (config.TOPIC_PH, {'ph':5.25,'source':'manual','sample_id':'test'}),
            (config.TOPIC_STATUS, {'mode':'HEATING','low':35.1,'high':35.6,'commanded_power':30}),
            (config.TOPIC_PELTIER_STATE, {'online':True,'applied_power':30,'max_power':30,'safety_latched':False}),
        ):
            monitor.handle_message(topic,json.dumps(payload).encode())
    elif archive=='legacy':
        monitor.log_file.write_text(f'timestamp,sensor,value\n{datetime.now().isoformat()},temperature,35.125\n')
    elif archive=='bad_schema':
        monitor.log_file.write_text('unexpected,header\n1,2\n')
    http=dashboard.app.server.test_client()
    for path in ('/', '/_dash-layout', '/_dash-dependencies'):
        assert http.get(path).status_code==200
    for key in dashboard.app.callback_map:
        response=callback_request(dashboard,key)
        assert response.status_code in (200,204), (key,response.data.decode())


def test_corrupt_sampling_history_does_not_prevent_startup(tmp_path):
    path=tmp_path/'sampling_events.csv'
    path.write_text('timestamp,event,reason,resume_at\n2026-10-15T10:00:00,PAUSE,pH,\n'
                    '2026-10-15T10:01:00,RESUME,pH,broken\n')
    monitor=MonitoringService(tmp_path/'r.csv',tmp_path/'f.csv',tmp_path/'b.csv')
    assert monitor.sampling_status()['paused']
    assert 'sampling history' in monitor.logging_status()


def test_logging_failure_does_not_stop_mqtt_and_success_clears_error(page, monkeypatch):
    dashboard,monitor=page
    original=monitor.logger.log_reading
    def broken(*args): raise OSError('No space left on device')
    monkeypatch.setattr(monitor.logger,'log_reading',broken)
    for value in (35.1,35.2):
        monitor._on_message(None,None,SimpleNamespace(topic=config.TOPIC_TEMPERATURE,
            payload=json.dumps({'temperature':value}).encode(),retain=False))
    assert monitor.latest['temperature'][0]==35.2
    assert 'No space left' in dashboard.refresh_logging(0)
    monkeypatch.setattr(monitor.logger,'log_reading',original)
    monitor._on_message(None,None,SimpleNamespace(topic=config.TOPIC_TEMPERATURE,
        payload=b'{"temperature":35.3}',retain=False))
    assert not monitor.logging_status()


def test_failed_sampling_write_does_not_claim_pause(page, monkeypatch):
    _,monitor=page
    def broken(*args): raise PermissionError('Read-only filesystem')
    monkeypatch.setattr(monitor.logger,'log_sampling_event',broken)
    assert 'unchanged' in monitor.set_sampling(True)
    assert not monitor.sampling_status()['paused']


def test_history_has_gap_during_sampling(page):
    dashboard,monitor=page
    now=datetime.now()
    for seconds in (10,8,6,4):
        monitor.logger.log_reading(now-timedelta(seconds=seconds),'temperature',35.1)
    monitor.logger.log_sampling_event(monitor.sampling_file,now-timedelta(seconds=9),'PAUSE','pH')
    monitor.logger.log_sampling_event(monitor.sampling_file,now-timedelta(seconds=5),'RESUME','pH')
    figure=dashboard.history_fig('temperature',60)
    assert figure.data[0].connectgaps is False
    assert any(value!=value for value in figure.data[0].y)


def test_oled_accepts_numeric_string_driver_feedback(page):
    from pi.oled import screen_lines
    _,monitor=page
    monitor.connected=True
    monitor.handle_message(config.TOPIC_PELTIER_STATE,
        b'{"online":true,"applied_power":"30","safety_latched":false}')
    assert 'Peltier: 30%' in screen_lines(monitor)


def test_sampling_buttons_over_http(page):
    dashboard, monitor = page
    response = callback_request(dashboard, 'sampling-action.children',
        {'pause-analysis': 1}, ['pause-analysis.n_clicks'])
    assert response.status_code == 200
    assert monitor.sampling_status()['paused']
    response = callback_request(dashboard, 'sampling-action.children',
        {'resume-analysis': 1, 'settle-seconds': 0}, ['resume-analysis.n_clicks'])
    assert response.status_code == 200
    assert not monitor.sampling_status()['paused']


def test_corrupt_bubble_file_does_not_break_temperature_history(page):
    dashboard, monitor = page
    monitor.logger.log_reading(datetime.now()-timedelta(seconds=1), 'temperature', 35.125)
    monitor.bubble_file.write_text('bad,header\n1,2\n')
    figure = dashboard.history_fig('temperature', 60)
    assert list(figure.data[0].y) == [35.125]


def test_report_download_retains_decimals_and_manual_ph_while_paused(page):
    dashboard, monitor = page
    now = datetime.now()-timedelta(seconds=10)
    monitor.logger.log_reading(now, 'temperature', 35.125)
    monitor.logger.log_sampling_event(monitor.sampling_file, now+timedelta(seconds=1), 'PAUSE', 'pH')
    monitor.logger.log_reading(now+timedelta(seconds=2), 'temperature', 34.875, excluded=True)
    monitor.logger.log_reading(now+timedelta(seconds=3), 'ph', 5.25, source='manual')
    response = callback_request(dashboard, 'report-download.data',
        {'download-report': 1, 'report-sensors': ['temperature', 'ph']}, ['download-report.n_clicks'])
    assert response.status_code == 200
    content = response.get_json()['response']['report-download']['data']['content']
    assert '35.125' in content and '5.25' in content
    assert '34.875' not in content


def test_bubble_rates_distinguish_quiet_outage_sampling_and_duplicate_events():
    import pandas as pd
    from monitoring.reporting import bubble_rate
    start = pd.Timestamp('2026-10-15T10:00:00')
    end = start+pd.Timedelta(minutes=4)
    events = pd.DataFrame([
        {'timestamp': start+pd.Timedelta(seconds=10), 'session': 'one', 'count': 1},
        {'timestamp': start+pd.Timedelta(seconds=10), 'session': 'one', 'count': 1},
        {'timestamp': start+pd.Timedelta(seconds=20), 'session': 'one', 'count': 2},
    ])
    coverage = pd.DataFrame({'timestamp': pd.date_range(start, start+pd.Timedelta(seconds=119), freq='1s'), 'available': True})
    result = bubble_rate(events, coverage, start, end, exclusions=[(start+pd.Timedelta(minutes=3), end)])
    assert list(result.value[:2]) == [2, 0]
    assert result.value[2:].isna().all()
