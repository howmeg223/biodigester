"""Live reception must keep working while archive/disk work is delayed."""
import csv
from datetime import datetime
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

import config
from monitoring.archive_cache import ArchiveCache
from monitoring.monitoring_service import MonitoringService


@pytest.fixture
def running_monitor(tmp_path, monkeypatch):
    client = Mock()
    client.is_connected.return_value = True
    monkeypatch.setattr('monitoring.monitoring_service.make_client', lambda _: client)
    monitor = MonitoringService(tmp_path/'readings.csv', tmp_path/'faults.csv', tmp_path/'bubbles.csv')
    monitor.start()
    client.on_connect(client, None, None, 0)
    try:
        yield monitor
    finally:
        monitor.stop()


def receive(monitor, value, retained=False):
    monitor._on_message(None, None, SimpleNamespace(topic=config.TOPIC_TEMPERATURE,
        payload=f'{{"temperature":{value}}}'.encode(), retain=retained))


def drain(monitor):
    pending = monitor._log_queue
    with pending.all_tasks_done:
        assert pending.all_tasks_done.wait_for(lambda: pending.unfinished_tasks == 0, timeout=3)


def test_blocked_csv_writer_does_not_block_live_readings(running_monitor, monkeypatch):
    monitor = running_monitor
    entered, release = threading.Event(), threading.Event()
    original = monitor.logger.log_reading
    def delayed(*args):
        entered.set()
        assert release.wait(3)
        original(*args)
    monkeypatch.setattr(monitor.logger, 'log_reading', delayed)
    try:
        receive(monitor, 29.125)
        assert entered.wait(1)
        receive(monitor, 29.25)
        receive(monitor, 29.375)
        assert monitor.snapshot()[0]['temperature'][0] == 29.375
        assert monitor._log_queue.qsize() == 2
    finally:
        release.set()
    drain(monitor)
    with monitor.log_file.open(newline='') as stream:
        assert [float(row['value']) for row in csv.DictReader(stream)] == [29.125, 29.25, 29.375]


def test_archive_parsing_does_not_block_receiver_or_writer(running_monitor, monkeypatch):
    import dashboard
    monitor = running_monitor
    monkeypatch.setattr(dashboard, 'monitor', monitor)
    monkeypatch.setattr(dashboard, 'archive_cache', ArchiveCache())
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    received = threading.Event()
    original = dashboard.read_table
    def delayed(*args, **kwargs):
        # Simulate parsing after the short file snapshot lock is released.
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return result
    monkeypatch.setattr(dashboard, 'read_table', delayed)
    def read_archive():
        try:
            dashboard.read_analysis_files(False)
        finally:
            completed.set()
    reader = threading.Thread(target=read_archive)
    reader.start()
    receiver = None
    try:
        assert entered.wait(1)
        def incoming():
            receive(monitor, 30.125)
            received.set()
        receiver = threading.Thread(target=incoming)
        receiver.start()
        assert received.wait(1), 'Archive parser blocked the MQTT receiver'
        assert monitor.snapshot()[0]['temperature'][0] == 30.125
        drain(monitor)
        assert '30.125' in monitor.log_file.read_text()
    finally:
        release.set()
        reader.join(3)
        if receiver is not None:
            receiver.join(3)
    assert completed.is_set() and not reader.is_alive()


def test_archive_cache_shares_loads_and_refresh_reads_new_rows(tmp_path):
    now = [0]
    cache = ArchiveCache(10, clock=lambda: now[0])
    path = tmp_path/'data.csv'
    path.write_text('old')
    loader = Mock(return_value=pd.DataFrame({'value': [1]}))
    first = cache.read(path, ['value'], loader)
    path.write_text('new longer content')
    assert cache.read(path, ['value'], loader) is first
    loader.assert_called_once()
    loader.return_value = pd.DataFrame({'value': [2]})
    assert cache.read(path, ['value'], loader, refresh=True).value.iloc[0] == 2
    assert loader.call_count == 2
    now[0] = 11
    cache.read(path, ['value'], loader)
    assert loader.call_count == 2  # Unchanged files are not reparsed.


def test_receiver_processing_exception_is_visible_and_next_message_works(running_monitor, monkeypatch):
    monitor = running_monitor
    original = monitor.handle_message
    monkeypatch.setattr(monitor, 'handle_message', Mock(side_effect=RuntimeError('test processing failure')))
    receive(monitor, 30)
    assert 'test processing failure' in monitor.logging_status()['MQTT receiver']
    monkeypatch.setattr(monitor, 'handle_message', original)
    receive(monitor, 30.5)
    assert monitor.latest['temperature'][0] == 30.5
    assert monitor.receiver_status()['error'] is None
    drain(monitor)


def test_writer_exception_does_not_stop_next_save(running_monitor, monkeypatch):
    monitor = running_monitor
    original = monitor.logger.log_reading
    monkeypatch.setattr(monitor.logger, 'log_reading', Mock(side_effect=RuntimeError('test disk failure')))
    receive(monitor, 30)
    drain(monitor)
    assert 'test disk failure' in monitor.logging_status()['readings']
    monkeypatch.setattr(monitor.logger, 'log_reading', original)
    receive(monitor, 30.5)
    drain(monitor)
    assert not monitor.logging_status()
    assert '30.5' in monitor.log_file.read_text()


def test_queue_full_reports_data_loss_and_keeps_latest(running_monitor, monkeypatch):
    monitor = running_monitor
    entered, release = threading.Event(), threading.Event()
    original = monitor.logger.log_reading
    def delayed(*args):
        entered.set()
        assert release.wait(3)
        original(*args)
    monkeypatch.setattr(monitor.logger, 'log_reading', delayed)
    # Change the limit before any work is submitted, without replacing the queue
    # the running worker is waiting on.
    monitor._log_queue.maxsize = 1
    try:
        receive(monitor, 30)
        assert entered.wait(1)
        receive(monitor, 31)
        receive(monitor, 32)
        assert monitor.snapshot()[0]['temperature'][0] == 32
        assert 'not saved' in monitor.logging_status()['queue']
    finally:
        release.set()
    drain(monitor)
    assert 'queue' in monitor.logging_status()  # Do not hide a lost-record warning.


def test_retained_reading_is_not_logged_by_background_writer(running_monitor):
    receive(running_monitor, 30, retained=True)
    drain(running_monitor)
    assert not running_monitor.log_file.exists()
    assert running_monitor.latest['temperature'][2] is True


def test_live_callbacks_never_load_archives_and_graphs_use_slow_tick(running_monitor, monkeypatch):
    import dashboard
    monkeypatch.setattr(dashboard, 'monitor', running_monitor)
    monkeypatch.setattr(dashboard, 'read_analysis_files', Mock(side_effect=AssertionError('Live callback read CSV')))
    receive(running_monitor, 30.125)
    values, stamps, status = dashboard.refresh_cards(1)
    assert '30.12 °C' in values
    assert 'Connected' in status
    dashboard.refresh_ph_status(1, None)
    for output, spec in dashboard.app.callback_map.items():
        if 'hist-graph.figure' in output or output == 'ph-graph.figure':
            assert any(i['id'] == 'graph-tick' for i in spec['inputs'])
            assert all(i['id'] != 'tick' for i in spec['inputs'])


def test_explicit_report_refresh_bypasses_cached_archive(tmp_path, monkeypatch):
    import dashboard
    monitor = MonitoringService(tmp_path/'r.csv', tmp_path/'f.csv', tmp_path/'b.csv')
    monkeypatch.setattr(dashboard, 'monitor', monitor)
    monkeypatch.setattr(dashboard, 'archive_cache', ArchiveCache(1000))
    monitor.logger.log_reading(datetime(2026, 10, 17, 10), 'temperature', 30)
    assert list(dashboard.report_data(['temperature'])[0].value) == [30]
    monitor.logger.log_reading(datetime(2026, 10, 17, 11), 'temperature', 31)
    assert list(dashboard.report_data(['temperature'], refresh=True)[0].value) == [30, 31]
