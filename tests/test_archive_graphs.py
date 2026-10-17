"""Regression checks for archived data, sparse points and partial coverage."""
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import config
from monitoring.monitoring_service import MonitoringService
from monitoring.reporting import bubble_rate, read_table


@pytest.fixture
def archive_page(tmp_path, monkeypatch):
    import dashboard
    monitor=MonitoringService(tmp_path/'readings.csv',tmp_path/'faults.csv',tmp_path/'bubbles.csv')
    monkeypatch.setattr(dashboard,'monitor',monitor)
    monkeypatch.setattr(dashboard,'lock',monitor.lock)
    return dashboard,monitor


def test_latest_logged_window_uses_sensor_archive_not_wall_clock(archive_page):
    dashboard,monitor=archive_page
    for hour,value in ((10,35.125),(11,35.25)):
        monitor.logger.log_reading(datetime(2020,1,1,hour), 'temperature',value)
    figure=dashboard.history_fig('temperature','latest')
    assert [value for value in figure.data[0].y if pd.notna(value)]==[35.125,35.25]


def test_latest_ph_window_uses_ph_measurement_date(archive_page):
    dashboard,monitor=archive_page
    monitor.logger.log_reading(datetime(2020,1,1,10),'ph',5.25,'manual')
    monitor.logger.log_reading(datetime(2026,10,17,10),'temperature',35)
    _,figure,_=dashboard.refresh_ph(0,None,'Latest logged 24 hours')
    assert list(figure.data[0].y)==[5.25]


def test_events_survive_partial_coverage_but_unknown_quiet_is_not_zero():
    start=pd.Timestamp('2026-10-17T10:00:00')
    events=pd.DataFrame({'timestamp':[start+pd.Timedelta(seconds=10)],'session':['test'],'count':[1]})
    coverage=pd.DataFrame({'timestamp':[start],'available':[True]})
    result=bubble_rate(events,coverage,start,start+pd.Timedelta(minutes=2),1)
    assert result.value.iloc[0]==1
    assert result.coverage_confirmed.iloc[0]==False
    assert 'incomplete' in result.source.iloc[0]
    assert pd.isna(result.value.iloc[1])


def test_single_temperature_measurement_is_visible(archive_page):
    dashboard,monitor=archive_page
    monitor.logger.log_reading(datetime(2020,1,1,10),'temperature',35.125)
    figure=dashboard.history_fig('temperature',None)
    assert 'markers' in figure.data[0].mode
    assert list(figure.data[0].y)==[35.125]


def test_missing_sensor_archive_is_identified(archive_page):
    dashboard,monitor=archive_page
    message=dashboard.refresh_archive_status(0)
    assert str(monitor.log_file) in message
    assert 'missing' in message.lower()


def test_auto_interval_does_not_hide_committed_bubble_archive(archive_page):
    dashboard,monitor=archive_page
    data=Path(__file__).resolve().parents[1]/'data'
    monitor.bubble_file=data/'bubble_events.csv'
    monitor.coverage_file=data/'bubble_coverage.csv'
    events=read_table(monitor.bubble_file,['timestamp','session','count'])
    if events.empty: pytest.skip('No committed event archive')
    result,_,start,end=dashboard.report_data(['bubble_rate'],interval='auto')
    assert len(result)<=config.REPORT_MAX_BINS
    assert result['count'].sum()==events.drop_duplicates(['session','count']).shape[0]
    assert result.loc[result['count'].gt(0),'value'].notna().all()
    figure=dashboard.report_fig(['bubble_rate'],None,interval='auto')
    assert figure.data and 'markers' in figure.data[0].mode


def test_explicit_too_fine_interval_still_reports_limit(archive_page):
    dashboard,monitor=archive_page
    monitor.logger.log_reading(datetime(2020,1,1),'temperature',35)
    with pytest.raises(ValueError,match='too large'):
        dashboard.report_data(['bubble_rate'],start='2020-01-01',end='2021-01-01',interval=1)


def test_partial_sampling_period_stays_excluded():
    start=pd.Timestamp('2026-10-17T10:00:00')
    events=pd.DataFrame({'timestamp':[start+pd.Timedelta(seconds=10)],'session':['test'],'count':[1]})
    coverage=pd.DataFrame(columns=['timestamp','available'])
    result=bubble_rate(events,coverage,start,start+pd.Timedelta(minutes=1),1,
        exclusions=[(start,start+pd.Timedelta(seconds=20))])
    assert result.value.isna().all()


def test_all_data_includes_last_archived_timestamp(archive_page):
    dashboard,monitor=archive_page
    stamp=datetime(2030,1,1,10)
    monitor.logger.log_reading(stamp,'temperature',35.125)
    data,_,_,end=dashboard.report_data(['temperature'])
    assert list(data.value)==[35.125]
    assert end>stamp


def test_selecting_new_history_range_resets_previous_plot_zoom(archive_page):
    dashboard,monitor=archive_page
    monitor.logger.log_reading(datetime.now(),'temperature',35.125)
    recent=dashboard.history_fig('temperature',60)
    archive=dashboard.history_fig('temperature',None)
    assert recent.layout.uirevision != archive.layout.uirevision


def test_auto_interval_keeps_optional_config_compatibility(monkeypatch):
    monkeypatch.delattr(config,'REPORT_MAX_BINS')
    start=pd.Timestamp('2026-01-01')
    events=pd.DataFrame(columns=['timestamp','session','count'])
    coverage=pd.DataFrame(columns=['timestamp','available'])
    result=bubble_rate(events,coverage,start,start+pd.Timedelta(days=90),'auto')
    assert len(result)<=20000
