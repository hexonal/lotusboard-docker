#!/usr/bin/env python3
"""Exercise a real promotion and rollback while probing all ingress paths."""
from collections import Counter
import hashlib
import json
import threading
import time
import urllib.request
import manage as m


def main():
    active = m.load()['active']
    target = next(slot for slot in m.SLOTS if slot != active)
    done = threading.Event()
    probes, transfers = [], []
    endpoints = {
        'host84': 'http://127.0.0.1:84',
        'public-tunnel': 'https://v2-proxy.996icu.wiki',
        'public-nginx': 'https://v2.996icu.wiki',
    }
    def probe(label, base):
        while not done.is_set():
            item = {'time': time.time(), 'entry': label}
            try:
                request = urllib.request.Request(base + '/api/v1/guest/comm/config?deployment_probe=' + str(time.time_ns()), headers={'Cache-Control': 'no-cache'})
                with urllib.request.urlopen(request, timeout=8) as response:
                    item.update(status=response.status, slot=response.headers.get('X-Deployment-Slot'))
                    assert isinstance(json.loads(response.read())['data'], dict)
            except Exception as exc:
                item['error'] = type(exc).__name__
            probes.append(item)
            done.wait(.2 if label == 'host84' else 1)
    def transfer():
        item = {'started': time.time()}
        try:
            expected = hashlib.sha256((m.SOURCE / 'public/assets/admin/umi.js').read_bytes()).hexdigest()
            request = urllib.request.Request(endpoints['host84'] + '/assets/admin/umi.js', headers={'Accept-Encoding': 'identity'})
            digest, length = hashlib.sha256(), 0
            with urllib.request.urlopen(request, timeout=15) as response:
                item['start_slot'] = response.headers.get('X-Deployment-Slot')
                while True:
                    chunk = response.read(16384)
                    if not chunk:
                        break
                    length += len(chunk)
                    digest.update(chunk)
                    time.sleep(.25)
            item.update(bytes=length, hash_matches=digest.hexdigest() == expected, duration=round(time.time() - item['started'], 2))
        except Exception as exc:
            item['error'] = type(exc).__name__
        transfers.append(item)
    threads = [threading.Thread(target=probe, args=entry) for entry in endpoints.items()]
    for thread in threads:
        thread.start()
    try:
        for slot in (target, active):
            slow = threading.Thread(target=transfer)
            slow.start()
            m.run('python3', str(m.ROOT / 'manage.py'), 'promote', slot)
            slow.join()
            print(json.dumps({'promoted': slot, 'slow_response': transfers[-1]}), flush=True)
    finally:
        done.set()
        for thread in threads:
            thread.join()
        failures = [p for p in probes if p.get('status') != 200 or p.get('error') or p.get('slot') not in m.SLOTS]
        result = {'samples': len(probes), 'failures': len(failures), 'observed': dict(Counter(p['entry'] + ':' + str(p.get('slot')) for p in probes)), 'transfers': transfers, 'probes': probes}
        m.atomic(m.ROOT / 'exercise-evidence.json', json.dumps(result, indent=2))
        print(json.dumps({k: v for k, v in result.items() if k != 'probes'}), flush=True)
    assert not failures and len(transfers) == 2 and all(t.get('hash_matches') for t in transfers)


if __name__ == '__main__':
    main()
