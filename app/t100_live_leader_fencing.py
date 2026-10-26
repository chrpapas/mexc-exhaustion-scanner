"""Durable leader fencing for a future T100 live executor.

No exchange calls. A live mutation coordinator must hold a fresh lease and
carry its monotonic fencing token before invoking any exchange POST.

The fencing token increases on takeover by a different holder. Renewing the
same holder preserves its token. An expired/stale holder cannot renew or
assert ownership.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class LeaderLeaseHalted(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaderLease:
    lease_name: str
    holder_id: str
    fencing_token: int
    ttl_seconds: int
    live_authorization: bool = False


class LeaseStore(Protocol):
    async def acquire(
        self, lease_name: str, holder_id: str, ttl_seconds: int
    ) -> tuple[int, bool]: ...
    async def renew(
        self, lease_name: str, holder_id: str,
        fencing_token: int, ttl_seconds: int
    ) -> bool: ...
    async def assert_current(
        self, lease_name: str, holder_id: str, fencing_token: int
    ) -> bool: ...
    async def release(
        self, lease_name: str, holder_id: str, fencing_token: int
    ) -> bool: ...


class LeaderLeaseCoordinator:
    def __init__(
        self,
        store: LeaseStore,
        *,
        lease_name: str = "t100-live-executor",
        ttl_seconds: int = 45,
    ) -> None:
        if (
            not isinstance(lease_name, str)
            or not lease_name
            or type(ttl_seconds) is not int
            or not 15 <= ttl_seconds <= 120
        ):
            raise ValueError("INVALID_LEADER_LEASE_CONFIGURATION")
        self.store = store
        self.lease_name = lease_name
        self.ttl_seconds = ttl_seconds

    async def acquire(self, holder_id: str) -> LeaderLease:
        self._holder(holder_id)
        try:
            token, acquired = await self.store.acquire(
                self.lease_name,
                holder_id,
                self.ttl_seconds,
            )
        except Exception as exc:
            raise LeaderLeaseHalted("LEADER_LEASE_STORE_FAILED") from exc
        if (
            acquired is not True
            or isinstance(token, bool)
            or not isinstance(token, int)
            or token <= 0
        ):
            raise LeaderLeaseHalted("LEADER_LEASE_NOT_ACQUIRED")
        return LeaderLease(
            lease_name=self.lease_name,
            holder_id=holder_id,
            fencing_token=token,
            ttl_seconds=self.ttl_seconds,
        )

    async def renew(self, lease: LeaderLease) -> LeaderLease:
        self._lease(lease)
        try:
            ok = await self.store.renew(
                lease.lease_name,
                lease.holder_id,
                lease.fencing_token,
                lease.ttl_seconds,
            )
        except Exception as exc:
            raise LeaderLeaseHalted("LEADER_LEASE_STORE_FAILED") from exc
        if ok is not True:
            raise LeaderLeaseHalted("LEADER_LEASE_LOST")
        return lease

    async def require_current(self, lease: LeaderLease) -> None:
        self._lease(lease)
        try:
            ok = await self.store.assert_current(
                lease.lease_name,
                lease.holder_id,
                lease.fencing_token,
            )
        except Exception as exc:
            raise LeaderLeaseHalted("LEADER_LEASE_STORE_FAILED") from exc
        if ok is not True:
            raise LeaderLeaseHalted("LEADER_FENCING_TOKEN_STALE")

    async def release(self, lease: LeaderLease) -> None:
        self._lease(lease)
        try:
            ok = await self.store.release(
                lease.lease_name,
                lease.holder_id,
                lease.fencing_token,
            )
        except Exception as exc:
            raise LeaderLeaseHalted("LEADER_LEASE_STORE_FAILED") from exc
        if ok is not True:
            raise LeaderLeaseHalted("LEADER_LEASE_RELEASE_FAILED")

    def _holder(self, holder_id: str) -> None:
        if not isinstance(holder_id, str) or not holder_id.strip():
            raise LeaderLeaseHalted("LEADER_HOLDER_ID_INVALID")

    def _lease(self, lease: LeaderLease) -> None:
        if (
            not isinstance(lease, LeaderLease)
            or lease.lease_name != self.lease_name
            or lease.ttl_seconds != self.ttl_seconds
            or not lease.holder_id
            or isinstance(lease.fencing_token, bool)
            or lease.fencing_token <= 0
        ):
            raise LeaderLeaseHalted("LEADER_LEASE_INVALID")


class PostgresLeaderLeaseStore:
    """PostgreSQL-backed lease with monotonic takeover fencing tokens."""

    def __init__(self, pool: Any):
        self.pool = pool

    async def acquire(
        self, lease_name: str, holder_id: str, ttl_seconds: int
    ) -> tuple[int, bool]:
        row = await self.pool.fetchrow(
            """INSERT INTO t100_live_executor_leases
               (lease_name,holder_id,fencing_token,lease_expires_at,updated_at)
               VALUES($1,$2,1,now()+($3 * interval '1 second'),now())
               ON CONFLICT (lease_name) DO UPDATE
               SET holder_id=EXCLUDED.holder_id,
                   fencing_token=CASE
                     WHEN t100_live_executor_leases.holder_id=EXCLUDED.holder_id
                     THEN t100_live_executor_leases.fencing_token
                     ELSE t100_live_executor_leases.fencing_token+1
                   END,
                   lease_expires_at=EXCLUDED.lease_expires_at,
                   updated_at=now()
               WHERE
                 t100_live_executor_leases.holder_id=EXCLUDED.holder_id
                 OR t100_live_executor_leases.lease_expires_at <= now()
               RETURNING fencing_token""",
            lease_name,
            holder_id,
            ttl_seconds,
        )
        if row is None:
            return 0, False
        return int(row["fencing_token"]), True

    async def renew(
        self,
        lease_name: str,
        holder_id: str,
        fencing_token: int,
        ttl_seconds: int,
    ) -> bool:
        row = await self.pool.fetchval(
            """UPDATE t100_live_executor_leases
               SET lease_expires_at=now()+($4 * interval '1 second'),
                   updated_at=now()
               WHERE lease_name=$1
                 AND holder_id=$2
                 AND fencing_token=$3
                 AND lease_expires_at > now()
               RETURNING fencing_token""",
            lease_name,
            holder_id,
            fencing_token,
            ttl_seconds,
        )
        return row is not None

    async def assert_current(
        self, lease_name: str, holder_id: str, fencing_token: int
    ) -> bool:
        row = await self.pool.fetchval(
            """SELECT 1
               FROM t100_live_executor_leases
               WHERE lease_name=$1
                 AND holder_id=$2
                 AND fencing_token=$3
                 AND lease_expires_at > now()""",
            lease_name,
            holder_id,
            fencing_token,
        )
        return row == 1

    async def release(
        self, lease_name: str, holder_id: str, fencing_token: int
    ) -> bool:
        row = await self.pool.fetchval(
            """UPDATE t100_live_executor_leases
               SET lease_expires_at=now(),updated_at=now()
               WHERE lease_name=$1
                 AND holder_id=$2
                 AND fencing_token=$3
               RETURNING fencing_token""",
            lease_name,
            holder_id,
            fencing_token,
        )
        return row is not None
