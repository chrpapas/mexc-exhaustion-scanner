from scripts.t100_position_pilot_preflight import inventory_empty


def test_empty_inventory_requires_complete_strict_counts():
    counts = dict.fromkeys(('sumCount', 'stopOrderCount', 'limitOrderCount',
                            'planOrderCount', 'trackOrderCount'), 0)
    assert inventory_empty([[], [], counts])
    for key in counts:
        for value in (None, False, '0', 1, -1):
            assert not inventory_empty([[], [], dict(counts, **{key: value})])
    assert not inventory_empty([[{}], [], counts])
    assert not inventory_empty([[], [{}], counts])
    assert not inventory_empty([None, [], counts])


def test_signed_get_transport_uses_current_domain_without_redirects(monkeypatch):
    from types import SimpleNamespace

    from scripts import t100_position_pilot_preflight as module

    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=b'fake-test-credential'))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    calls = []
    counts = dict.fromkeys(('sumCount', 'stopOrderCount', 'limitOrderCount',
                            'planOrderCount', 'trackOrderCount'), 0)

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['base_url'] == 'https://api.mexc.com'
            assert kwargs['follow_redirects'] is False
            assert kwargs['trust_env'] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, path, **kwargs):
            calls.append(path)
            data = counts if path.endswith('open_order_total_count') else []
            return SimpleNamespace(status_code=200,
                                   json=lambda: {'success': True, 'code': 0, 'data': data})

    monkeypatch.setattr(module.httpx, 'Client', Client)
    assert module.run()['status'] == 'EMPTY_INVENTORY_OBSERVED_TWICE'
    assert calls == list(module.PATHS) * 2
