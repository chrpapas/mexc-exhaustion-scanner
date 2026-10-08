import asyncio
from types import SimpleNamespace

from app.t100_200_worker import T100Worker


class FakePool:
    def __init__(self):
        self.saved_args = None

    async def fetchrow(self, query, symbol):
        return {
            "symbol": symbol,
            "started_at": None,
            "state": "run_watch",
            "peak_price": 1.0,
            "peak_at": None,
            "last_run_score": 1.0,
            "last_exhaustion_score": 0.0,
            "broken_level": None,
            "breakdown_at": None,
            "breakdown_atr7": None,
            "confirmed_at": None,
            # This mirrors asyncpg's default json/jsonb text representation.
            "metadata": '{"nested":"already-json-text"}',
            "updated_at": None,
        }

    async def execute(self, query, *args):
        self.saved_args = args
        return "INSERT 0 1"


def test_scanner_state_metadata_is_normalized_and_not_double_encoded():
    worker = object.__new__(T100Worker)
    pool = FakePool()
    worker.db = SimpleNamespace(pool=pool)

    state = asyncio.run(worker._load_scanner_state("TEST_USDT"))
    assert state["metadata"] == {}

    asyncio.run(worker._save_scanner_state(state))

    # The metadata bind parameter is the final argument and must stay canonical.
    assert pool.saved_args[-1] == "{}"
