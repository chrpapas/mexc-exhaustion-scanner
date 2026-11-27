"""Actual same-signer MEXC signed-GET/PG-select four-slot current-risk assembler.

Binds the existing restricted, SELECT-only V40 ledger; MEXC position,
native-stop, order-completeness, contract, private tier, FAIR and asset GET
adapters; and the current 1–4-slot risk bridge. The SAME injected signed
MEXC account client is used for both inventory and cross risk. A second
complete inventory/ledger read AFTER risk gathering rejects any detected
drift or stale cycle. All emitted results remain nonauthorizing.

The caller is responsible for independently proving API-key identity,
read-only role grants, current guardian health, historical strategy parity
and certified cross wallet/margin semantics; this module does NOT provide
any of those permissions or issue a permit.

No credentials loaded, POST, SQL write, order submit, stop modification,
release, migration or Render worker registration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.t100_live_four_slot_snapshot_coordinator import (
    FourSlotSnapshotCoordinator,_ledger_digest,
)
from app.t100_live_four_slot_ledger_reader import PostgresFourSlotLedgerReader
from app.t100_live_four_slot_mexc_readonly import (
    FourSlotMexcReadVenue,FourSlotMexcCompleteness,
)
from app.t100_live_cross_risk_collector import (
    MexcReadOnlyCrossRiskCollector,
)
from app.t100_live_cross_risk_observation import (
    CrossRiskRead,observe_cross_risk_window,
)
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_v40_unreleased_current_portfolio_risk import (
    V40PortfolioCurrentRisk,audit_current_four_slot_risk,
)


class V40SameSignerRiskHalted(ValueError):
    pass


@dataclass
class _CaptureCrossRiskRead:
    collector: MexcReadOnlyCrossRiskCollector
    latest: CrossRiskRead | None = None

    async def read(self) -> CrossRiskRead:
        value=await self.collector.read()
        if type(value) is not CrossRiskRead:
            raise V40SameSignerRiskHalted("V40_CAPTURED_CROSS_RISK_FRAME_INVALID")
        self.latest=value
        return value


class V40SameSignerCurrentRiskReader:
    """Composes actual signed GET readers; no mutation ports exist."""

    def __init__(
        self,*,ledger: PostgresFourSlotLedgerReader,
        private: ReadOnlyMexcProbe,
        public: Any,clock_ms: Callable[[],int],
        max_risk_read_age_ms: int = 5000,
        max_cycle_duration_ms: int = 15000,
    ) -> None:
        if (
            type(ledger) is not PostgresFourSlotLedgerReader
            or type(private) is not ReadOnlyMexcProbe
            or public is None or not callable(getattr(public,"get",None))
            or str(getattr(public,"base_url",None)).rstrip("/")
                != "https://api.mexc.com"
            or not callable(clock_ms)
            or type(max_risk_read_age_ms) is not int
            or not 500<=max_risk_read_age_ms<=5000
            or type(max_cycle_duration_ms) is not int
            or not 5000<=max_cycle_duration_ms<=20000
        ):
            raise V40SameSignerRiskHalted("V40_SAME_SIGNER_READER_CONFIG_INVALID")
        self.ledger=ledger
        self.private=private
        self.public=public
        self.clock_ms=clock_ms
        self.max_risk_age_ms=max_risk_read_age_ms
        self.max_cycle_ms=max_cycle_duration_ms

    async def observe_current(
        self,
    ) -> V40PortfolioCurrentRisk:
        block=lambda code: V40PortfolioCurrentRisk(code)
        try:
            start=self.clock_ms()
            if type(start) is not int or start<=0:
                return block("V40_SAME_SIGNER_CLOCK_INVALID")
            venue=FourSlotMexcReadVenue(self.private)
            completeness=FourSlotMexcCompleteness(self.private,venue)
            inventory=FourSlotSnapshotCoordinator(
                ledger=self.ledger,venue=venue,completeness=completeness,
            )
            first=await inventory.inspect()
            if first.status!="NONFLAT_PROVENANCE_MATCHED_NONAUTHORIZING":
                return block("V40_SAME_SIGNER_OWNED_ACCOUNT_NOT_PROVEN")
            prior_ledger=await self.ledger.read_snapshot()
            if _ledger_digest(prior_ledger)!=first.ledger_hash:
                return block("V40_SAME_SIGNER_LEDGER_CHANGED_BEFORE_RISK")
            reader=_CaptureCrossRiskRead(
                MexcReadOnlyCrossRiskCollector(
                    private=self.private,public=self.public,
                    clock_ms=self.clock_ms,
                    max_frame_ms=self.max_risk_age_ms,
                ),
            )
            risk=await observe_cross_risk_window(
                reader,now_ms=self.clock_ms,
                max_age_ms=self.max_risk_age_ms,
                max_separation_ms=self.max_risk_age_ms,
            )
            if (
                risk.status!="CURRENT_CROSS_RISK_OBSERVED_NONAUTHORIZING"
                or type(reader.latest) is not CrossRiskRead
            ):
                return block("V40_SAME_SIGNER_CURRENT_RISK_WINDOW_UNVERIFIED")
            final=await inventory.inspect()
            final_ledger=await self.ledger.read_snapshot()
            stop=self.clock_ms()
            if (
                type(stop) is not int
                or stop<start or stop-start>self.max_cycle_ms
                or final.status!=first.status
                or final.occupied_slots!=first.occupied_slots
                or final.ledger_hash!=first.ledger_hash
                or final.critical_inventory_hash!=first.critical_inventory_hash
                or _ledger_digest(final_ledger)!=first.ledger_hash
            ):
                return block("V40_SAME_SIGNER_POST_RISK_ACCOUNT_OR_LEDGER_DRIFT")
            # The last cross read is the only fair/risk frame allowed. Do
            # not substitute first-frame prices or cached prior observations.
            return audit_current_four_slot_risk(
                snapshot=final,ledger=final_ledger,
                risk_observation=risk,risk_read=reader.latest,
                now_ms=stop,max_age_ms=self.max_risk_age_ms,
            )
        except Exception:
            # Never surface raw GET/SQL errors or connection details.
            return block("V40_SAME_SIGNER_READ_OR_ATTESTATION_FAILED")
