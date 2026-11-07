"""Dedicated-subaccount GET-only preflight. No mutation transport or live flag."""
import hashlib
import hmac
import json
import subprocess
import time

import httpx

BASE_URL = 'https://api.mexc.com'

PATHS = (
    '/api/v1/private/position/open_positions',
    '/api/v1/private/stoporder/open_orders',
    '/api/v1/private/order/open_order_total_count',
)


def inventory_empty(rows):
    positions, stops, counts = rows
    fields = ('sumCount', 'stopOrderCount', 'limitOrderCount', 'planOrderCount', 'trackOrderCount')
    return (type(positions) is list and positions == [] and type(stops) is list and stops == []
            and type(counts) is dict
            and all(type(counts.get(k)) is int and counts[k] == 0 for k in fields))


def run():
    credentials = []
    for account in ('MEXC_API_KEY', 'MEXC_API_SECRET'):
        result = subprocess.run(
            ['/usr/bin/security', 'find-generic-password', '-s', 't100225-mexc-pilot-subaccount',
             '-a', account, '-w'], capture_output=True, timeout=15, check=False)
        if result.returncode or not result.stdout.strip():
            return {'status': 'KEYCHAIN_ACCESS_BLOCKED', 'post_sent': False}
        credentials.append(result.stdout.decode().strip())
    key, secret = credentials
    observations = []
    with httpx.Client(base_url=BASE_URL, timeout=12,
                      follow_redirects=False, trust_env=False) as client:
        for _ in range(2):
            rows = []
            for path in PATHS:
                stamp = str(int(time.time() * 1000))
                signature = hmac.new(secret.encode(), (key + stamp).encode(), hashlib.sha256).hexdigest()
                response = client.get(path, headers={'ApiKey': key, 'Request-Time': stamp,
                                                     'Signature': signature})
                if response.status_code != 200:
                    return {'status': 'SIGNED_GET_HTTP_FAILED', 'http_status': response.status_code, 'post_sent': False}
                body = response.json()
                if (type(body) is not dict or body.get('success') is not True
                        or type(body.get('code')) is not int or body['code'] != 0):
                    return {'status': 'SIGNED_GET_ENVELOPE_FAILED', 'post_sent': False}
                rows.append(body.get('data'))
                time.sleep(0.3)
            observations.append(rows)
            if not inventory_empty(rows):
                return {'status': 'UNEXPECTED_OR_UNVERIFIED_INVENTORY_HALT', 'post_sent': False}
    return {'status': 'EMPTY_INVENTORY_OBSERVED_TWICE', 'post_sent': False,
            'atomic_snapshot': False, 'live_authorization': False,
            'observed_at_unix': int(time.time())}


if __name__ == '__main__':
    try:
        report = run()
    except Exception:  # noqa: BLE001 - redact transport and subprocess details
        report = {'status': 'PREFLIGHT_FAILED_DETAILS_WITHHELD', 'post_sent': False}
    print(json.dumps(report, sort_keys=True))
