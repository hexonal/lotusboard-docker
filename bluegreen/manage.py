#!/usr/bin/env python3
"""Host-side release manager. No database migrations or live vendor writes."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state.json'
SOURCE = ROOT.parent / 'lotusboard'
SLOTS = ('blue', 'green')


def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, text=True, **kwargs)


def atomic(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(value)
    os.replace(temp, path)


def save(state):
    atomic(STATE, json.dumps(state, indent=2) + '\n')


def load():
    return json.loads(STATE.read_text())


def name(slot, role):
    return f'v2board-{role}-{slot}'


def compose(state):
    services = {}
    for slot, release in state['slots'].items():
        for role in ('web', 'worker', 'scheduler'):
            runtime = ROOT / 'runtime' / release['release'] / role
            (runtime / 'cache').mkdir(parents=True, exist_ok=True)
            (runtime / 'views').mkdir(exist_ok=True)
            volumes = [f"{ROOT / 'releases' / release['release']}:/www:ro",
                       f'{ROOT}:/opt/bluegreen:ro', f'{runtime}:/runtime',
                       f'{ROOT.parent / "wwwlogs"}:/wwwlogs',
                       f'{SOURCE / ".env"}:/www/.env:ro',
                       f'{SOURCE / "storage"}:/www/storage',
                       f'{SOURCE / "config/v2board.php"}:/www/config/v2board.php',
                       f'{SOURCE / "config/theme"}:/www/config/theme',
                       f'{SOURCE / "public/theme"}:/www/public/theme',
                       f'{SOURCE / "resources/rules"}:/www/resources/rules:ro',
                       f'{ROOT / "php.ini"}:/usr/local/etc/php/conf.d/zzz-bluegreen.ini:ro']
            environment = {'RUN_WORKERS': 'false', 'RUN_SCHEDULER': 'false',
                           'APP_DISABLE_CONFIG_CACHE': 'true',
                           'VIEW_COMPILED_PATH': '/runtime/views'}
            for kind in ('CONFIG', 'SERVICES', 'PACKAGES', 'ROUTES', 'EVENTS'):
                environment[f'APP_{kind}_CACHE'] = f'/runtime/cache/{kind.lower()}.php'
            service = dict(image=release['image'], container_name=name(slot, role),
                           hostname=name(slot, role), init=True, stop_grace_period='150s', stop_signal='SIGTERM',
                           restart='always' if role == 'web' or state.get('active') == slot else 'no',
                           networks=['frontend'], volumes=volumes, environment=environment)
            if role == 'web':
                service['command'] = ['supervisord', '-c', '/opt/bluegreen/web-supervisord.conf']
                health = 'curl -fsS --max-time 5 http://127.0.0.1/api/v1/guest/comm/config >/dev/null'
            elif role == 'worker':
                service['profiles'] = [f'background-{slot}']
                service['command'] = ['supervisord', '-c', '/opt/bluegreen/worker-supervisord.conf']
                health = "supervisorctl -c /opt/bluegreen/worker-supervisord.conf status | awk 'BEGIN {n=0} /RUNNING/ {n++} END {exit n != 5}'"
            else:
                service['profiles'] = [f'background-{slot}']
                environment['RUN_SCHEDULER'] = 'true'
                volumes.append(f'{ROOT / "crontab"}:/etc/crontabs/root:ro')
                service['command'] = ['sh', '/opt/bluegreen/scheduler.sh']
                health = 'php /opt/bluegreen/scheduler-health.php'
            service['healthcheck'] = dict(test=['CMD-SHELL', health], interval='10s', timeout='8s', start_period='70s' if role == 'scheduler' else '20s', retries=3)
            services[f'{role}-{slot}'] = service
    config = dict(name='lotusboard-bg', services=services,
                  networks={'frontend': {'external': True, 'name': 'docker-compose_frontend'}})
    atomic(ROOT / 'compose.json', json.dumps(config, indent=2) + '\n')


def dc(*args):
    run('docker', 'compose', '-p', 'lotusboard-bg', '-f', str(ROOT / 'compose.json'), *args)


def healthy(container):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = run('docker', 'inspect', '--format', '{{.State.Health.Status}}', container, capture_output=True)
        if result.stdout.strip() == 'healthy':
            return
        time.sleep(2)
    raise RuntimeError(f'{container} did not become healthy')


def verify(slot, gateway=False):
    cmd = ['docker', 'exec', '-i', '-w', '/www']
    if gateway:
        cmd += ['-e', 'RUNTIME_HTTP_BASE=http://v2board-new:80']
    cmd += [name(slot, 'web'), 'php']
    result = run(*cmd, input=(ROOT / 'verify.php').read_text(), capture_output=True)
    data = json.loads(result.stdout)
    assert data['result'] == 'PASS'
    print(json.dumps({'slot': slot, 'via_gateway': gateway, 'checks': len(data['checks']), 'result': 'PASS'}), flush=True)
    atomic(ROOT / f'verification-{slot}{"-gateway" if gateway else ""}.json', result.stdout)


def gateway_config(slot):
    return '''{
    admin 127.0.0.1:2019
    grace_period 120s
}
:80 {
    header X-Deployment-Slot SLOT
    reverse_proxy v2board-web-SLOT:80 {
        health_uri /api/v1/guest/comm/config
        health_interval 5s
        health_timeout 3s
        health_status 200
        trusted_proxies private_ranges
        transport http {
            keepalive 15s
        }
    }
}
'''.replace('SLOT', slot)


def reload_gateway(slot):
    directory = ROOT / 'gateway'
    directory.mkdir(exist_ok=True)
    candidate = directory / 'candidate.Caddyfile'
    candidate.write_text(gateway_config(slot))
    run('docker', 'exec', 'v2board-new', 'caddy', 'validate', '--config', '/etc/caddy/candidate.Caddyfile', '--adapter', 'caddyfile', capture_output=True)
    os.replace(candidate, directory / 'Caddyfile')
    run('docker', 'exec', 'v2board-new', 'caddy', 'reload', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile', capture_output=True)
    request = urllib.request.Request('http://127.0.0.1:84/api/v1/guest/comm/config', headers={'Host': 'v2-proxy.996icu.wiki'})
    with urllib.request.urlopen(request, timeout=15) as response:
        assert response.status == 200 and response.headers['X-Deployment-Slot'] == slot


def stop_background(slot):
    # Disable resurrection before stopping, including after an interrupted deploy.
    for role in ('scheduler', 'worker'):
        container = name(slot, role)
        if subprocess.run(['docker', 'inspect', container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            run('docker', 'update', '--restart=no', container, stdout=subprocess.DEVNULL)
            run('docker', 'stop', '--signal', 'SIGTERM', '-t', '150', container, stdout=subprocess.DEVNULL)
            code = run('docker', 'inspect', '--format', '{{.State.ExitCode}}', container, capture_output=True).stdout.strip()
            if code == '137' or (role == 'scheduler' and code != '0'):
                raise RuntimeError(f'{container} failed graceful shutdown (exit {code})')


def start_background(slot):
    dc('up', '-d', f'worker-{slot}')
    healthy(name(slot, 'worker'))
    dc('up', '-d', f'scheduler-{slot}')
    healthy(name(slot, 'scheduler'))
    for role in ('worker', 'scheduler'):
        run('docker', 'update', '--restart=always', name(slot, role), stdout=subprocess.DEVNULL)


def promote(state, slot):
    compose(state)
    old = state.get('active')
    if old == slot:
        verify(slot, True)
        return
    healthy(name(slot, 'web'))
    verify(slot)
    atomic(ROOT / 'handoff.json', json.dumps({'from': old, 'to': slot, 'started': time.time()}))
    try:
        if old:
            stop_background(old)
        reload_gateway(slot)
        start_background(slot)
        verify(slot, True)
        state['active'] = slot
        save(state)
        compose(state)
        (ROOT / 'handoff.json').unlink()
    except BaseException:
        stop_background(slot)
        if old:
            reload_gateway(old)
            start_background(old)
            compose(state)
            (ROOT / 'handoff.json').unlink(missing_ok=True)
        raise


def prepare(state, slot, release, image, source=None):
    source = source or SOURCE
    image_id = run('docker', 'image', 'inspect', '--format', '{{.Id}}', image, capture_output=True).stdout.strip()
    if slot == state.get('active'):
        raise RuntimeError('Refusing to overwrite the active slot')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', release):
        raise ValueError('Invalid release identifier')
    destination = ROOT / 'releases' / release
    if destination.exists():
        raise RuntimeError('Release already exists; use a new immutable release identifier')
    # Old web must have no in-flight HTTP connections before replacing its bind mount.
    container = name(slot, 'web')
    inspect = subprocess.run(['docker', 'inspect', container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if inspect.returncode == 0:
        probe = "awk '$4 == \"01\" && $2 ~ /:0050$/ {n++} END {exit n>0}' /proc/net/tcp /proc/net/tcp6"
        deadline = time.monotonic() + 150
        while subprocess.run(['docker', 'exec', container, 'sh', '-c', probe], stdout=subprocess.DEVNULL).returncode:
            if time.monotonic() > deadline:
                raise RuntimeError(f'{container} still has HTTP connections; preserving the old release')
            time.sleep(2)
        stop_background(slot)
    def ignore(path, names):
        relative = Path(path).relative_to(source)
        skipped = {'.git', '.env', 'core'}
        if relative == Path('.'):
            skipped |= {'storage'}
        if relative == Path('bootstrap/cache'):
            skipped |= {n for n in names if n != '.gitignore'}
        return [n for n in names if n in skipped or n.startswith('core.')]
    destination.parent.mkdir(exist_ok=True)
    shutil.copytree(source, destination, symlinks=True, ignore=ignore)
    for directory in ('storage', 'config/theme', 'public/theme', 'resources/rules'):
        (destination / directory).mkdir(parents=True, exist_ok=True)
    (destination / '.env').touch()
    (destination / 'config/v2board.php').touch(exist_ok=True)
    state['slots'][slot] = {'release': release, 'image': image_id, 'image_tag': image}
    save(state)
    compose(state)
    dc('up', '-d', f'web-{slot}')
    healthy(container)
    run('docker', 'exec', '-w', '/www', container, 'composer', 'check-platform-reqs', '--no-dev', capture_output=True)
    verify(slot)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('slot', choices=SLOTS)
    p.add_argument('release')
    p.add_argument('--image', default='ix-lotusboard:php84-20261109')
    p.add_argument('--source', type=Path, help='Prepared source + vendor tree; defaults to the legacy production tree')
    p = sub.add_parser('promote')
    p.add_argument('slot', choices=SLOTS)
    sub.add_parser('status')
    sub.add_parser('recover', help='Restore the persisted active slot after an interrupted handoff')
    p = sub.add_parser('verify')
    p.add_argument('slot', choices=SLOTS)
    p.add_argument('--gateway', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    with (ROOT / '.deploy.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load() if STATE.exists() else {'active': None, 'slots': {}}
        if (ROOT / 'handoff.json').exists() and args.command not in ('recover', 'status'):
            raise RuntimeError('Interrupted handoff found; run recover before preparing or promoting')
        if args.command == 'prepare':
            prepare(state, args.slot, args.release, args.image, args.source.resolve() if args.source else None)
        elif args.command == 'promote':
            promote(state, args.slot)
        elif args.command == 'verify':
            verify(args.slot, args.gateway)
        elif args.command == 'recover':
            slot = state['active']
            if not slot:
                raise RuntimeError('No committed active slot; inspect the initial migration backup')
            for candidate in SLOTS:
                stop_background(candidate)
            reload_gateway(slot)
            start_background(slot)
            verify(slot, True)
            compose(state)
            (ROOT / 'handoff.json').unlink(missing_ok=True)
        else:
            print(json.dumps(state, indent=2))
            dc('ps', '-a')


if __name__ == '__main__':
    main()
