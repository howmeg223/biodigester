#!/usr/bin/env python3
"""Install the accompanying payload without replacing the user's hardware config.

Run with the Pi's virtualenv Python. Does not send power or reset safety latches.
"""
import argparse
import ast
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

FILES = (
    'mqtt_common.py', 'pi/peltier.py',
    'controller_system/biodigester_controller.py',
    'controller_system/fault_manager.py',
    'simulation/devices.py', 'simulation/simulator_service.py', 'dashboard.py',
)
VERSION = 'pwm-cap-v2'
SERVICES = ('biodigester@controller.service', 'biodigester@peltier.service')


def configure_power(source, power=30.0):
    """Change only power assignments; preserve calibration and pinouts verbatim."""
    names = {'PELTIER_MAX_POWER', 'PELTIER_HEAT_POWER'}
    lines = source.splitlines(keepends=True)
    edits, found = [], set()
    for node in ast.parse(source).body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        matches = [t.id for t in targets if isinstance(t, ast.Name) and t.id in names]
        if not matches:
            continue
        if len(targets) != 1 or len(matches) != 1:
            raise ValueError('Power settings must use separate assignments in config.py')
        name = matches[0]
        edits.append((node.lineno - 1, node.end_lineno, f'{name} = {power!r}\n'))
        found.add(name)
    for start, end, value in reversed(edits):
        lines[start:end] = [value]
    result = ''.join(lines).rstrip() + '\n'
    for name in sorted(names - found):
        result += f'{name} = {power!r}\n'
    compile(result, 'config.py', 'exec')
    return result


def systemctl(*args):
    return subprocess.run(['sudo', 'systemctl', *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def verify_live(project, timeout=15, require_controller=True):
    """Require fresh reports from both updated processes, not saved broker state."""
    sys.path.insert(0, str(project))
    import config
    import paho.mqtt.client as mqtt
    expected = {
        config.TOPIC_PELTIER_STATE: ('driver_version', 'applied_power'),
    }
    if require_controller:
        expected[config.TOPIC_STATUS] = ('power_control_version', 'commanded_power')
    seen, errors = {}, []
    done = threading.Event()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f'power-install-check-{time.time_ns()}')
    if config.MQTT_USERNAME is not None:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)

    def connected(client, userdata, flags, reason, properties):
        if reason.is_failure:
            errors.append(f'MQTT connection rejected: {reason}')
            done.set()
        else:
            for topic in expected:
                client.subscribe(topic, qos=1)

    def received(client, userdata, msg):
        if msg.retain or msg.topic not in expected:
            return
        try:
            data = json.loads(msg.payload)
            if data.get('online') is False or data.get('mode') == 'OFFLINE':
                return
            version_key, power_key = expected[msg.topic]
            if data.get(version_key) != VERSION or data.get('max_power') != 30:
                raise ValueError(f'Older or conflicting process on {msg.topic}: {data}')
            power = data[power_key]
            if isinstance(power, bool) or not isinstance(power, (int, float)) or not -30 <= power <= 30:
                raise ValueError(f'Output exceeds limit on {msg.topic}: {data}')
            if msg.topic == config.TOPIC_PELTIER_STATE:
                if Path(data['driver_file']).resolve() != project / 'pi/peltier.py':
                    raise ValueError(f'Driver loaded from another checkout: {data}')
                if Path(data['config_file']).resolve() != project / 'config.py':
                    raise ValueError(f'Driver loaded another configuration: {data}')
            seen[msg.topic] = data
            if len(seen) == len(expected):
                done.set()
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
            done.set()

    client.on_connect, client.on_message = connected, received
    try:
        client.connect(config.BROKER_HOST, config.BROKER_PORT, keepalive=30)
        client.loop_start()
        if not done.wait(timeout):
            raise RuntimeError(f'No fresh verified reports from both services. Received: {seen}')
        if errors:
            raise RuntimeError('; '.join(errors))
        # Continue briefly to detect another publisher using these same topics.
        time.sleep(2)
        if errors:
            raise RuntimeError('; '.join(errors))
        print(json.dumps(seen, indent=2))
    finally:
        client.disconnect()
        client.loop_stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.home() / 'biodigester')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    project = args.project.resolve()
    if args.verify_only:
        verify_live(project)
        return
    payload = Path(__file__).resolve().parent / 'payload'
    python = project / '.venv/bin/python'
    if not python.is_file():
        raise RuntimeError(f'Virtualenv Python not found: {python}')
    # Check dependencies before stopping the existing services.
    subprocess.run([str(python), '-c', 'import paho.mqtt.client'], check=True)
    if Path(sys.executable).absolute() != python.absolute():
        raise RuntimeError(f'Run this installer with {python}, not {sys.executable}')
    for service in SERVICES:
        directory = systemctl('show', service, '-p', 'WorkingDirectory', '--value')
        if not directory or Path(directory).resolve() != project:
            raise RuntimeError(f'{service} uses WorkingDirectory={directory!r}; expected {project}')
    files = {name: (payload / name).read_bytes() for name in FILES}
    files['config.py'] = configure_power((project / 'config.py').read_text()).encode()
    for name, contents in files.items():
        compile(contents, name, 'exec')
    # Import the staged config and shared power helper in a clean process.
    with tempfile.TemporaryDirectory(prefix='biodigester-preflight-') as stage:
        for name in ('config.py', 'mqtt_common.py'):
            (Path(stage) / name).write_bytes(files[name])
        subprocess.run([str(python), '-B', '-c',
            'from mqtt_common import *; '
            'assert peltier_heating_power() == 30; '
            'assert limit_peltier_power(100) == 30; '
            'assert limit_peltier_power(-100) == -30'], cwd=stage, check=True)
    backup = project / ('power-update-backup-' + time.strftime('%Y%m%d-%H%M%S'))
    backup.mkdir()
    for name in files:
        original = project / name
        if original.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, saved)
    print(f'Backup: {backup}', flush=True)
    systemctl('stop', *SERVICES)
    try:
        for name, contents in files.items():
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(target.name + '.power-update')
            staged.write_bytes(contents)
            staged.replace(target)
        # Avoid same-second Python bytecode cache reuse after replacing config.
        for name in files:
            target = project / name
            for pyc in (target.parent / '__pycache__').glob(target.stem + '.*.pyc'):
                pyc.unlink()
        systemctl('reset-failed', *SERVICES)
        systemctl('start', 'biodigester@peltier.service')
        # Verify the limiting driver while the automatic controller is stopped.
        verify_live(project, require_controller=False)
        systemctl('start', 'biodigester@controller.service')
        verify_live(project)
    except Exception:
        systemctl('stop', *SERVICES)
        print('Verification failed. Controller and Peltier services left stopped. '
              f'Backup: {backup}', file=sys.stderr)
        raise
    # Dashboard must reload its code to show the live limit and source.
    if systemctl('show', 'biodigester@dashboard.service', '-p', 'ActiveState', '--value') == 'active':
        systemctl('restart', 'biodigester@dashboard.service')
    print('Verified: both running processes use pwm-cap-v2, maximum duty 30%. '
          'No manual power command or safety reset was sent.')


if __name__ == '__main__':
    main()
