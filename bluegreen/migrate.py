#!/usr/bin/env python3
"""One-time migration of the original all-in-one service to the stable gateway."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import yaml
import manage as m


def main():
    os.umask(0o077)
    with (m.ROOT / '.deploy.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = m.load()
        assert state['active'] is None and set(state['slots']) == {'blue', 'green'}
        for slot in m.SLOTS:
            m.healthy(m.name(slot, 'web'))
            m.verify(slot)
        original = json.loads(m.run('docker', 'inspect', 'v2board-new', capture_output=True).stdout)[0]
        assert original['Config']['Image'] == 'ix-lotusboard:php84-20261109'
        network = original['NetworkSettings']['Networks']['docker-compose_frontend']
        assert network['IPAddress'] == '172.18.0.9' and network['GlobalIPv6Address'] == '2001:db8::9'
        path = Path('/root/docker-compose/docker-compose.yaml')
        old = path.read_text()
        config = yaml.safe_load(old)
        assert config['services']['v2board-new']['image'] == original['Config']['Image']
        backup = m.ROOT / 'backups' / time.strftime('%Y%m%dT%H%M%S')
        backup.mkdir(parents=True)
        shutil.copy2(path, backup / 'docker-compose.yaml')
        (backup / 'original-inspect.json').write_text(json.dumps(original, indent=2))
        (backup / 'location.txt').write_text(str(path))
        (m.ROOT / 'gateway/Caddyfile').write_text(m.gateway_config('blue'))
        service = dict(image=original['Config']['Image'], container_name='v2board-new',
                       hostname='v2board-new', restart='always',
                       entrypoint=['caddy'], command=['run', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'],
                       ports=['84:80'], volumes=[f'{m.ROOT / "gateway"}:/etc/caddy:ro'],
                       networks={'frontend': {'ipv4_address': '172.18.0.9', 'ipv6_address': '2001:db8::9'}},
                       stop_grace_period='150s', stop_signal='SIGTERM',
                       healthcheck=dict(test=['CMD-SHELL', 'curl -fsS --max-time 5 http://127.0.0.1/api/v1/guest/comm/config >/dev/null'], interval='10s', timeout='8s', start_period='10s', retries=3))
        block = yaml.safe_dump({'v2board-new': service}, sort_keys=False)
        block = ''.join('  ' + line if line.strip() else line for line in block.splitlines(True))
        updated, count = re.subn(r'(?ms)^  v2board-new:\n.*?(?=^  [\w-]+:|^[^\s#])', block, old)
        assert count == 1
        actual = yaml.safe_load(updated)
        assert actual['services'].pop('v2board-new') == service
        expected = yaml.safe_load(old)
        expected['services'].pop('v2board-new')
        assert actual == expected, 'Unrelated Compose configuration changed'
        candidate = path.with_name('bluegreen-candidate.yaml')
        candidate.write_text(updated)
        m.run('docker', 'compose', '-p', 'docker-compose', '-f', str(candidate), 'config', '--quiet')
        probes = []
        done = threading.Event()
        def probe():
            while not done.is_set():
                started = time.time()
                try:
                    with urllib.request.urlopen('http://127.0.0.1:84/api/v1/guest/comm/config', timeout=3) as response:
                        probes.append({'time': started, 'status': response.status})
                except Exception as exc:
                    probes.append({'time': started, 'error': type(exc).__name__})
                done.wait(.2)
        thread = threading.Thread(target=probe)
        thread.start()
        switched = False
        try:
            # Scheduler must stop creating work before existing invocations are drained.
            m.run('docker', 'exec', 'v2board-new', 'supervisorctl', '-c', '/etc/supervisor/lotusboard.conf', 'stop', 'cron')
            deadline = time.monotonic() + 120
            while subprocess.run(['docker', 'exec', 'v2board-new', 'pgrep', '-f', 'artisan schedule:run'], stdout=subprocess.DEVNULL).returncode == 0:
                if time.monotonic() > deadline:
                    raise RuntimeError('Legacy scheduler did not drain')
                time.sleep(1)
            m.run('docker', 'exec', 'v2board-new', 'supervisorctl', '-c', '/etc/supervisor/lotusboard.conf', 'stop', 'horizon', 'v2board:*')
            m.atomic(path, updated)
            switched = True
            m.run('docker', 'compose', '-p', 'docker-compose', '-f', str(path), 'up', '-d', '--no-deps', 'v2board-new')
            m.healthy('v2board-new')
            m.run('docker', 'exec', 'nginx', 'nginx', '-t')
            m.run('docker', 'exec', 'nginx', 'nginx', '-s', 'reload')
            m.reload_gateway('blue')
            m.start_background('blue')
            m.verify('blue', True)
            state['active'] = 'blue'
            state['legacy_backup'] = str(backup)
            m.save(state)
            m.compose(state)
        except BaseException:
            m.stop_background('blue')
            if switched:
                m.atomic(path, old)
                m.run('docker', 'compose', '-p', 'docker-compose', '-f', str(path), 'up', '-d', '--no-deps', 'v2board-new')
            else:
                m.run('docker', 'exec', 'v2board-new', 'supervisorctl', '-c', '/etc/supervisor/lotusboard.conf', 'start', 'cron', 'horizon', 'v2board:*')
            raise
        finally:
            done.set()
            thread.join()
            candidate.unlink(missing_ok=True)
            evidence = {'stage': 'initial-migration', 'samples': len(probes), 'failures': sum(p.get('status') != 200 for p in probes), 'probes': probes}
            m.atomic(m.ROOT / 'migration-evidence.json', json.dumps(evidence, indent=2))
            print(json.dumps({k: v for k, v in evidence.items() if k != 'probes'}), flush=True)


if __name__ == '__main__':
    main()
