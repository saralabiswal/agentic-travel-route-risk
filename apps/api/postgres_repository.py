"""Async PostgreSQL repository for RouteShield's tenant-scoped operational records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, MetaData, String, Table, and_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from apps.api.security import redact
from domain.models import (
    ActionDispatchAttempt,
    ActionDispatchRecord,
    ActionDispatchStatus,
    ApprovalRecord,
    ChangeRecord,
    DeletionRequest,
    DeletionRequestScope,
    DeletionRequestStatus,
    EventRecord,
    EvidenceEnvelope,
    IdempotencyRecord,
    IdempotencyState,
    Incident,
    LegalHoldRecord,
    LegalHoldScope,
    ManagerFeedback,
    MemoryAuditEvent,
    MemoryUpdateProposal,
    NotificationAttempt,
    NotificationRecord,
    NotificationStatus,
    OriginalUploadRecord,
    PlatformRuntimeControlChange,
    ProviderOnboardingRecord,
    RiskAssessment,
    RuntimeControlChange,
    RuntimeControlName,
    TenantPlaybook,
    TravelerPreferenceProfile,
    Trip,
)
from domain.recovery import RecoveryCandidateOutcome, RecoveryCandidateSet

metadata = MetaData()
trips = Table(
    "trips",
    metadata,
    Column("trip_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
trip_segments = Table(
    "trip_segments",
    metadata,
    Column("trip_id", PGUUID(as_uuid=True), primary_key=True),
    Column("segment_id", String(64), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("carrier_code", String(3), nullable=False),
    Column("flight_number", String(4), nullable=False),
    Column("departure_airport", String(3), nullable=False),
    Column("arrival_airport", String(3), nullable=False),
    Column("scheduled_departure_at", DateTime(timezone=True), nullable=False, index=True),
    Column("scheduled_arrival_at", DateTime(timezone=True), nullable=False),
)
evidence_items = Table(
    "evidence_items",
    metadata,
    Column("evidence_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("trip_id", PGUUID(as_uuid=True), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
assessments = Table(
    "risk_assessments",
    metadata,
    Column("assessment_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("trip_id", PGUUID(as_uuid=True), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
incidents = Table(
    "incidents",
    metadata,
    Column("incident_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("trip_id", PGUUID(as_uuid=True), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
preferences = Table(
    "traveler_preference_profiles",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("traveler_id", String(128), primary_key=True),
    Column("payload", JSONB, nullable=False),
)
approvals = Table(
    "approvals",
    metadata,
    Column("incident_id", PGUUID(as_uuid=True), primary_key=True),
    Column("payload", JSONB, nullable=False),
)
events = Table(
    "audit_events",
    metadata,
    Column("event_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
memory_proposals = Table(
    "memory_update_proposals",
    metadata,
    Column("proposal_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
candidate_sets = Table(
    "recovery_candidate_sets",
    metadata,
    Column("candidate_set_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("incident_id", PGUUID(as_uuid=True), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
candidate_outcomes = Table(
    "recovery_candidate_outcomes",
    metadata,
    Column("outcome_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("incident_id", PGUUID(as_uuid=True), nullable=False, index=True),
    Column("candidate_set_id", PGUUID(as_uuid=True), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
manager_feedback = Table(
    "manager_feedback",
    metadata,
    Column("feedback_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("incident_id", PGUUID(as_uuid=True), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
tenant_playbooks = Table(
    "tenant_playbooks",
    metadata,
    Column("playbook_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
notifications = Table(
    "notifications",
    metadata,
    Column("notification_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
notification_attempts = Table(
    "notification_attempts",
    metadata,
    Column("attempt_id", PGUUID(as_uuid=True), primary_key=True),
    Column("notification_id", PGUUID(as_uuid=True), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
action_dispatches = Table(
    "action_dispatches",
    metadata,
    Column("action_dispatch_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("incident_id", PGUUID(as_uuid=True), nullable=False, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
action_dispatch_attempts = Table(
    "action_dispatch_attempts",
    metadata,
    Column("attempt_id", PGUUID(as_uuid=True), primary_key=True),
    Column("action_dispatch_id", PGUUID(as_uuid=True), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
memory_audit_events = Table(
    "memory_audit_events",
    metadata,
    Column("audit_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("traveler_id", String(128), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
change_records = Table(
    "change_records",
    metadata,
    Column("change_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("change_type", String(32), nullable=False),
    Column("payload", JSONB, nullable=False),
)
provider_onboarding_records = Table(
    "provider_onboarding_records",
    metadata,
    Column("provider_onboarding_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("status", String(32), nullable=False),
    Column("payload", JSONB, nullable=False),
)
runtime_control_overrides = Table(
    "runtime_control_overrides",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("control_name", String(64), primary_key=True),
    Column("payload", JSONB, nullable=False),
)
runtime_control_changes = Table(
    "runtime_control_changes",
    metadata,
    Column("control_change_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("control_name", String(64), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
platform_runtime_control_overrides = Table(
    "platform_runtime_control_overrides",
    metadata,
    Column("control_name", String(64), primary_key=True),
    Column("payload", JSONB, nullable=False),
)
platform_runtime_control_changes = Table(
    "platform_runtime_control_changes",
    metadata,
    Column("control_change_id", PGUUID(as_uuid=True), primary_key=True),
    Column("control_name", String(64), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
idempotency_records = Table(
    "idempotency_records",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("idempotency_key", String(256), primary_key=True),
    Column("payload", JSONB, nullable=False),
)
original_uploads = Table(
    "original_uploads",
    metadata,
    Column("original_upload_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
legal_holds = Table(
    "legal_holds",
    metadata,
    Column("legal_hold_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)
deletion_requests = Table(
    "deletion_requests",
    metadata,
    Column("deletion_request_id", PGUUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("traveler_id", String(128), nullable=False, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
)


def as_json(model: object) -> dict[str, object]:
    return model.model_dump(mode="json")  # type: ignore[union-attr]


class PostgresRouteShieldRepository:
    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)

    async def setup(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def create_trip(self, trip: Trip) -> Trip:
        async with self.engine.begin() as connection:
            await connection.execute(
                trips.insert().values(
                    trip_id=trip.trip_id, tenant_id=trip.tenant_id, payload=as_json(trip)
                )
            )
            await self._replace_trip_segments(connection, trip)
        return trip

    async def save_trip(self, trip: Trip) -> Trip:
        async with self.engine.begin() as connection:
            await connection.execute(
                pg_insert(trips)
                .values(trip_id=trip.trip_id, tenant_id=trip.tenant_id, payload=as_json(trip))
                .on_conflict_do_update(
                    index_elements=[trips.c.trip_id],
                    set_={"tenant_id": trip.tenant_id, "payload": as_json(trip)},
                )
            )
            await self._replace_trip_segments(connection, trip)
        return trip

    @staticmethod
    async def _replace_trip_segments(connection, trip: Trip) -> None:
        await connection.execute(
            trip_segments.delete().where(trip_segments.c.trip_id == trip.trip_id)
        )
        await connection.execute(
            trip_segments.insert(),
            [
                {
                    "trip_id": trip.trip_id,
                    "segment_id": segment.segment_id,
                    "tenant_id": trip.tenant_id,
                    "carrier_code": segment.carrier_code,
                    "flight_number": segment.flight_number,
                    "departure_airport": segment.departure_airport,
                    "arrival_airport": segment.arrival_airport,
                    "scheduled_departure_at": segment.scheduled_departure_at,
                    "scheduled_arrival_at": segment.scheduled_arrival_at,
                }
                for segment in trip.segments
            ],
        )

    async def get_trip(self, tenant_id: str, trip_id: UUID) -> Trip | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(trips.c.payload).where(
                        trips.c.tenant_id == tenant_id, trips.c.trip_id == trip_id
                    )
                )
            ).scalar_one_or_none()
        return Trip.model_validate(row) if row else None

    async def list_trips(self) -> list[Trip]:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(trips.c.payload))).scalars().all()
        return [Trip.model_validate(row) for row in rows]

    async def save_evidence(
        self, tenant_id: str, trip_id: UUID, evidence: list[EvidenceEnvelope]
    ) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                evidence_items.delete().where(
                    evidence_items.c.tenant_id == tenant_id, evidence_items.c.trip_id == trip_id
                )
            )
            if evidence:
                await connection.execute(
                    evidence_items.insert(),
                    [
                        {
                            "evidence_id": item.evidence_id,
                            "tenant_id": tenant_id,
                            "trip_id": trip_id,
                            "payload": as_json(item),
                        }
                        for item in evidence
                    ],
                )

    async def get_evidence(self, tenant_id: str, trip_id: UUID) -> list[EvidenceEnvelope]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(evidence_items.c.payload).where(
                            evidence_items.c.tenant_id == tenant_id,
                            evidence_items.c.trip_id == trip_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [EvidenceEnvelope.model_validate(row) for row in rows]

    async def save_assessment(self, tenant_id: str, assessment: RiskAssessment) -> RiskAssessment:
        async with self.engine.begin() as connection:
            await connection.execute(
                assessments.insert().values(
                    assessment_id=assessment.assessment_id,
                    tenant_id=tenant_id,
                    trip_id=assessment.trip_id,
                    payload=as_json(assessment),
                )
            )
        return assessment

    async def save_incident(self, incident: Incident) -> Incident:
        async with self.engine.begin() as connection:
            await connection.execute(
                pg_insert(incidents)
                .values(
                    incident_id=incident.incident_id,
                    tenant_id=incident.tenant_id,
                    trip_id=incident.trip_id,
                    payload=as_json(incident),
                )
                .on_conflict_do_update(
                    index_elements=[incidents.c.incident_id],
                    set_={"payload": as_json(incident)},
                )
            )
        return incident

    async def get_incident(self, tenant_id: str, incident_id: UUID) -> Incident | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(incidents.c.payload).where(
                        incidents.c.tenant_id == tenant_id, incidents.c.incident_id == incident_id
                    )
                )
            ).scalar_one_or_none()
        return Incident.model_validate(row) if row else None

    async def list_incidents(self, tenant_id: str) -> list[Incident]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(incidents.c.payload).where(incidents.c.tenant_id == tenant_id)
                    )
                )
                .scalars()
                .all()
            )
        return [Incident.model_validate(row) for row in rows]

    async def save_profile(self, profile: TravelerPreferenceProfile) -> TravelerPreferenceProfile:
        async with self.engine.begin() as connection:
            await connection.execute(
                pg_insert(preferences)
                .values(
                    tenant_id=profile.tenant_id,
                    traveler_id=profile.traveler_id,
                    payload=as_json(profile),
                )
                .on_conflict_do_update(
                    index_elements=[preferences.c.tenant_id, preferences.c.traveler_id],
                    set_={"payload": as_json(profile)},
                )
            )
        return profile

    async def get_profile(
        self, tenant_id: str, traveler_id: str
    ) -> TravelerPreferenceProfile | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(preferences.c.payload).where(
                        preferences.c.tenant_id == tenant_id,
                        preferences.c.traveler_id == traveler_id,
                    )
                )
            ).scalar_one_or_none()
        return TravelerPreferenceProfile.model_validate(row) if row else None

    async def save_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        async with self.engine.begin() as connection:
            await connection.execute(
                pg_insert(approvals)
                .values(incident_id=approval.incident_id, payload=as_json(approval))
                .on_conflict_do_update(
                    index_elements=[approvals.c.incident_id], set_={"payload": as_json(approval)}
                )
            )
        return approval

    async def claim_idempotency(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        scope: str,
        request_hash: str,
        expires_at: datetime,
    ) -> tuple[str, IdempotencyRecord]:
        record = IdempotencyRecord(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            scope=scope,
            request_hash=request_hash,
            expires_at=expires_at,
        )
        async with self.engine.begin() as connection:
            inserted = (
                await connection.execute(
                    pg_insert(idempotency_records)
                    .values(
                        tenant_id=tenant_id,
                        idempotency_key=idempotency_key,
                        payload=as_json(record),
                    )
                    .on_conflict_do_nothing()
                    .returning(idempotency_records.c.payload)
                )
            ).scalar_one_or_none()
            if inserted:
                return "execute", IdempotencyRecord.model_validate(inserted)
            current_payload = (
                await connection.execute(
                    select(idempotency_records.c.payload)
                    .where(
                        idempotency_records.c.tenant_id == tenant_id,
                        idempotency_records.c.idempotency_key == idempotency_key,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            current = IdempotencyRecord.model_validate(current_payload)
            if current.expires_at <= datetime.now(UTC):
                await connection.execute(
                    idempotency_records.update()
                    .where(
                        idempotency_records.c.tenant_id == tenant_id,
                        idempotency_records.c.idempotency_key == idempotency_key,
                    )
                    .values(payload=as_json(record))
                )
                return "execute", record
            if current.scope != scope or current.request_hash != request_hash:
                return "conflict", current
            if current.state == IdempotencyState.COMPLETED:
                return "replay", current
            return "in_progress", current

    async def complete_idempotency(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        request_hash: str,
        response_status_code: int,
        response_payload: dict[str, object],
    ) -> IdempotencyRecord:
        async with self.engine.begin() as connection:
            current_payload = (
                await connection.execute(
                    select(idempotency_records.c.payload)
                    .where(
                        idempotency_records.c.tenant_id == tenant_id,
                        idempotency_records.c.idempotency_key == idempotency_key,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if not current_payload:
                raise KeyError("idempotency claim not found")
            record = IdempotencyRecord.model_validate(current_payload)
            if record.request_hash != request_hash:
                raise KeyError("idempotency claim does not match request")
            record.state = IdempotencyState.COMPLETED
            record.response_status_code = response_status_code
            record.response_payload = response_payload
            record.completed_at = datetime.now(UTC)
            await connection.execute(
                idempotency_records.update()
                .where(
                    and_(
                        idempotency_records.c.tenant_id == tenant_id,
                        idempotency_records.c.idempotency_key == idempotency_key,
                    )
                )
                .values(payload=as_json(record))
            )
        return record

    async def record_event(self, event: EventRecord) -> EventRecord:
        event = event.model_copy(update={"details": redact(event.details)})
        async with self.engine.begin() as connection:
            await connection.execute(
                events.insert().values(
                    event_id=event.event_id, tenant_id=event.tenant_id, payload=as_json(event)
                )
            )
        return event

    async def list_events(self, tenant_id: str) -> list[EventRecord]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(events.c.payload).where(events.c.tenant_id == tenant_id)
                    )
                )
                .scalars()
                .all()
            )
        return [EventRecord.model_validate(row) for row in rows]

    async def save_memory_proposal(self, proposal: MemoryUpdateProposal) -> MemoryUpdateProposal:
        async with self.engine.begin() as connection:
            await connection.execute(
                pg_insert(memory_proposals)
                .values(
                    proposal_id=proposal.proposal_id,
                    tenant_id=proposal.tenant_id,
                    payload=as_json(proposal),
                )
                .on_conflict_do_update(
                    index_elements=[memory_proposals.c.proposal_id],
                    set_={"payload": as_json(proposal)},
                )
            )
        return proposal

    async def get_memory_proposal(
        self, tenant_id: str, proposal_id: UUID
    ) -> MemoryUpdateProposal | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(memory_proposals.c.payload).where(
                        memory_proposals.c.tenant_id == tenant_id,
                        memory_proposals.c.proposal_id == proposal_id,
                    )
                )
            ).scalar_one_or_none()
        return MemoryUpdateProposal.model_validate(row) if row else None

    async def delete_traveler_memory(self, tenant_id: str, traveler_id: str) -> dict[str, int]:
        async with self.engine.begin() as connection:
            profile_result = await connection.execute(
                preferences.delete().where(
                    preferences.c.tenant_id == tenant_id,
                    preferences.c.traveler_id == traveler_id,
                )
            )
            proposal_result = await connection.execute(
                memory_proposals.delete().where(
                    memory_proposals.c.tenant_id == tenant_id,
                    memory_proposals.c.payload["traveler_id"].astext == traveler_id,
                )
            )
        return {
            "profiles": profile_result.rowcount or 0,
            "memory_proposals": proposal_result.rowcount or 0,
        }

    async def save_candidate_set(
        self, tenant_id: str, candidate_set: RecoveryCandidateSet
    ) -> RecoveryCandidateSet:
        async with self.engine.begin() as connection:
            await connection.execute(
                candidate_sets.insert().values(
                    candidate_set_id=candidate_set.candidate_set_id,
                    tenant_id=tenant_id,
                    incident_id=candidate_set.incident_id,
                    payload=as_json(candidate_set),
                )
            )
        return candidate_set

    async def get_candidate_set(
        self, tenant_id: str, incident_id: UUID, candidate_set_id: UUID
    ) -> RecoveryCandidateSet | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(candidate_sets.c.payload).where(
                        candidate_sets.c.tenant_id == tenant_id,
                        candidate_sets.c.incident_id == incident_id,
                        candidate_sets.c.candidate_set_id == candidate_set_id,
                    )
                )
            ).scalar_one_or_none()
        return RecoveryCandidateSet.model_validate(row) if row else None

    async def list_candidate_sets(
        self, tenant_id: str, incident_id: UUID
    ) -> list[RecoveryCandidateSet]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(candidate_sets.c.payload).where(
                            candidate_sets.c.tenant_id == tenant_id,
                            candidate_sets.c.incident_id == incident_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [RecoveryCandidateSet.model_validate(row) for row in rows]

    async def record_candidate_outcome(
        self, tenant_id: str, outcome: RecoveryCandidateOutcome
    ) -> RecoveryCandidateOutcome:
        async with self.engine.begin() as connection:
            await connection.execute(
                candidate_outcomes.insert().values(
                    outcome_id=outcome.outcome_id,
                    tenant_id=tenant_id,
                    incident_id=outcome.incident_id,
                    candidate_set_id=outcome.candidate_set_id,
                    payload=as_json(outcome),
                )
            )
        return outcome

    async def list_candidate_outcomes(
        self, tenant_id: str, candidate_set_id: UUID
    ) -> list[RecoveryCandidateOutcome]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(candidate_outcomes.c.payload).where(
                            candidate_outcomes.c.tenant_id == tenant_id,
                            candidate_outcomes.c.candidate_set_id == candidate_set_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return sorted(
            (RecoveryCandidateOutcome.model_validate(row) for row in rows),
            key=lambda outcome: (outcome.occurred_at, str(outcome.outcome_id)),
        )

    async def save_manager_feedback(self, feedback: ManagerFeedback) -> ManagerFeedback:
        async with self.engine.begin() as connection:
            await connection.execute(
                manager_feedback.insert().values(
                    feedback_id=feedback.feedback_id,
                    tenant_id=feedback.tenant_id,
                    incident_id=feedback.incident_id,
                    payload=as_json(feedback),
                )
            )
        return feedback

    async def list_manager_feedback(
        self, tenant_id: str, incident_id: UUID
    ) -> list[ManagerFeedback]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(manager_feedback.c.payload).where(
                            manager_feedback.c.tenant_id == tenant_id,
                            manager_feedback.c.incident_id == incident_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return sorted(
            (ManagerFeedback.model_validate(row) for row in rows),
            key=lambda feedback: (feedback.created_at, str(feedback.feedback_id)),
        )

    async def get_playbook(self, tenant_id: str, playbook_id: UUID) -> TenantPlaybook | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(tenant_playbooks.c.payload).where(
                        tenant_playbooks.c.tenant_id == tenant_id,
                        tenant_playbooks.c.playbook_id == playbook_id,
                    )
                )
            ).scalar_one_or_none()
        return TenantPlaybook.model_validate(row) if row else None

    async def save_playbook(self, playbook: TenantPlaybook) -> TenantPlaybook:
        async with self.engine.begin() as connection:
            inserted = (
                await connection.execute(
                    pg_insert(tenant_playbooks)
                    .values(
                        playbook_id=playbook.playbook_id,
                        tenant_id=playbook.tenant_id,
                        payload=as_json(playbook),
                    )
                    .on_conflict_do_nothing()
                    .returning(tenant_playbooks.c.payload)
                )
            ).scalar_one_or_none()
            if inserted:
                return TenantPlaybook.model_validate(inserted)
            existing = (
                await connection.execute(
                    select(tenant_playbooks.c.payload).where(
                        tenant_playbooks.c.playbook_id == playbook.playbook_id
                    )
                )
            ).scalar_one()
        return TenantPlaybook.model_validate(existing)

    async def list_approved_playbooks(self, tenant_id: str) -> list[TenantPlaybook]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(tenant_playbooks.c.payload).where(
                            tenant_playbooks.c.tenant_id == tenant_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        return sorted(
            (TenantPlaybook.model_validate(row) for row in rows),
            key=lambda playbook: (playbook.name, playbook.version),
        )

    async def save_notification(self, notification: NotificationRecord) -> NotificationRecord:
        async with self.engine.begin() as connection:
            await connection.execute(
                pg_insert(notifications)
                .values(
                    notification_id=notification.notification_id,
                    tenant_id=notification.tenant_id,
                    status=notification.status.value,
                    payload=as_json(notification),
                )
                .on_conflict_do_update(
                    index_elements=[notifications.c.notification_id],
                    set_={
                        "status": notification.status.value,
                        "payload": as_json(notification),
                    },
                )
            )
        return notification

    async def get_notification(
        self, tenant_id: str, notification_id: UUID
    ) -> NotificationRecord | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(notifications.c.payload).where(
                        notifications.c.tenant_id == tenant_id,
                        notifications.c.notification_id == notification_id,
                    )
                )
            ).scalar_one_or_none()
        return NotificationRecord.model_validate(row) if row else None

    async def list_notifications(
        self, tenant_id: str, statuses: set[NotificationStatus] | None = None
    ) -> list[NotificationRecord]:
        statement = select(notifications.c.payload).where(notifications.c.tenant_id == tenant_id)
        if statuses:
            statement = statement.where(
                notifications.c.status.in_([item.value for item in statuses])
            )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).scalars().all()
        return [NotificationRecord.model_validate(row) for row in rows]

    async def list_due_notifications(self, now: datetime | None = None) -> list[NotificationRecord]:
        timestamp = now or datetime.now(UTC)
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(notifications.c.payload).where(
                        notifications.c.status.in_(
                            [
                                NotificationStatus.QUEUED.value,
                                NotificationStatus.RETRY_SCHEDULED.value,
                            ]
                        )
                    )
                )
            ).scalars().all()
        return [
            notification
            for notification in (NotificationRecord.model_validate(row) for row in rows)
            if notification.next_attempt_at <= timestamp
        ]

    async def claim_due_notifications(
        self,
        *,
        tenant_id: str | None,
        worker_id: str,
        claim_ttl: timedelta,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[NotificationRecord]:
        """Use row locks to lease due JSON outbox records without duplicate workers."""
        timestamp = now or datetime.now(UTC)
        statement = select(notifications.c.notification_id, notifications.c.payload).where(
            notifications.c.status.in_(
                [NotificationStatus.QUEUED.value, NotificationStatus.RETRY_SCHEDULED.value]
            )
        )
        if tenant_id:
            statement = statement.where(notifications.c.tenant_id == tenant_id)
        statement = statement.with_for_update(skip_locked=True).limit(limit)
        claimed: list[NotificationRecord] = []
        async with self.engine.begin() as connection:
            rows = (await connection.execute(statement)).all()
            for row in rows:
                notification = NotificationRecord.model_validate(row.payload)
                if notification.next_attempt_at > timestamp:
                    continue
                if (
                    notification.dispatch_claim_expires_at
                    and notification.dispatch_claim_expires_at > timestamp
                ):
                    continue
                leased = notification.model_copy(
                    update={
                        "dispatch_claim_id": uuid4(),
                        "dispatch_claimed_by": worker_id,
                        "dispatch_claimed_at": timestamp,
                        "dispatch_claim_expires_at": timestamp + claim_ttl,
                    }
                )
                await connection.execute(
                    notifications.update()
                    .where(notifications.c.notification_id == notification.notification_id)
                    .values(payload=as_json(leased))
                )
                claimed.append(leased)
        return claimed

    async def complete_notification_delivery(
        self,
        *,
        notification: NotificationRecord,
        attempt: NotificationAttempt,
        claim_id: UUID,
    ) -> NotificationRecord | None:
        async with self.engine.begin() as connection:
            payload = (
                await connection.execute(
                    select(notifications.c.payload)
                    .where(notifications.c.notification_id == notification.notification_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if not payload:
                return None
            current = NotificationRecord.model_validate(payload)
            if current.dispatch_claim_id != claim_id:
                return None
            completed = notification.model_copy(
                update={
                    "dispatch_claim_id": None,
                    "dispatch_claimed_by": None,
                    "dispatch_claimed_at": None,
                    "dispatch_claim_expires_at": None,
                }
            )
            await connection.execute(
                notifications.update()
                .where(notifications.c.notification_id == notification.notification_id)
                .values(status=completed.status.value, payload=as_json(completed))
            )
            await connection.execute(
                notification_attempts.insert().values(
                    attempt_id=attempt.attempt_id,
                    notification_id=attempt.notification_id,
                    payload=as_json(attempt),
                )
            )
        return completed

    async def record_notification_attempt(
        self, attempt: NotificationAttempt
    ) -> NotificationAttempt:
        async with self.engine.begin() as connection:
            await connection.execute(
                notification_attempts.insert().values(
                    attempt_id=attempt.attempt_id,
                    notification_id=attempt.notification_id,
                    payload=as_json(attempt),
                )
            )
        return attempt

    async def list_notification_attempts(
        self, tenant_id: str, notification_id: UUID
    ) -> list[NotificationAttempt]:
        if not await self.get_notification(tenant_id, notification_id):
            return []
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(notification_attempts.c.payload).where(
                        notification_attempts.c.notification_id == notification_id
                    )
                )
            ).scalars().all()
        return [NotificationAttempt.model_validate(row) for row in rows]

    async def save_action_dispatch(self, action: ActionDispatchRecord) -> ActionDispatchRecord:
        async with self.engine.begin() as connection:
            await connection.execute(
                pg_insert(action_dispatches)
                .values(
                    action_dispatch_id=action.action_dispatch_id,
                    tenant_id=action.tenant_id,
                    incident_id=action.incident_id,
                    status=action.status.value,
                    payload=as_json(action),
                )
                .on_conflict_do_update(
                    index_elements=[action_dispatches.c.action_dispatch_id],
                    set_={"status": action.status.value, "payload": as_json(action)},
                )
            )
        return action

    async def list_action_dispatches(
        self, tenant_id: str, incident_id: UUID
    ) -> list[ActionDispatchRecord]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(action_dispatches.c.payload).where(
                            action_dispatches.c.tenant_id == tenant_id,
                            action_dispatches.c.incident_id == incident_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [ActionDispatchRecord.model_validate(row) for row in rows]

    async def claim_due_action_dispatches(
        self,
        *,
        tenant_id: str | None,
        worker_id: str,
        claim_ttl: timedelta,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[ActionDispatchRecord]:
        timestamp = now or datetime.now(UTC)
        statement = select(
            action_dispatches.c.action_dispatch_id, action_dispatches.c.payload
        ).where(
            action_dispatches.c.status.in_(
                [ActionDispatchStatus.QUEUED.value, ActionDispatchStatus.RETRY_SCHEDULED.value]
            )
        )
        if tenant_id:
            statement = statement.where(action_dispatches.c.tenant_id == tenant_id)
        statement = statement.with_for_update(skip_locked=True).limit(limit)
        claimed: list[ActionDispatchRecord] = []
        async with self.engine.begin() as connection:
            rows = (await connection.execute(statement)).all()
            for row in rows:
                action = ActionDispatchRecord.model_validate(row.payload)
                if action.next_attempt_at > timestamp:
                    continue
                if (
                    action.dispatch_claim_expires_at
                    and action.dispatch_claim_expires_at > timestamp
                ):
                    continue
                leased = action.model_copy(
                    update={
                        "dispatch_claim_id": uuid4(),
                        "dispatch_claimed_by": worker_id,
                        "dispatch_claimed_at": timestamp,
                        "dispatch_claim_expires_at": timestamp + claim_ttl,
                    }
                )
                await connection.execute(
                    action_dispatches.update()
                    .where(action_dispatches.c.action_dispatch_id == action.action_dispatch_id)
                    .values(payload=as_json(leased))
                )
                claimed.append(leased)
        return claimed

    async def complete_action_dispatch(
        self,
        *,
        action: ActionDispatchRecord,
        attempt: ActionDispatchAttempt,
        claim_id: UUID,
    ) -> ActionDispatchRecord | None:
        async with self.engine.begin() as connection:
            payload = (
                await connection.execute(
                    select(action_dispatches.c.payload)
                    .where(action_dispatches.c.action_dispatch_id == action.action_dispatch_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if not payload:
                return None
            current = ActionDispatchRecord.model_validate(payload)
            if current.dispatch_claim_id != claim_id:
                return None
            completed = action.model_copy(
                update={
                    "dispatch_claim_id": None,
                    "dispatch_claimed_by": None,
                    "dispatch_claimed_at": None,
                    "dispatch_claim_expires_at": None,
                }
            )
            await connection.execute(
                action_dispatches.update()
                .where(action_dispatches.c.action_dispatch_id == action.action_dispatch_id)
                .values(status=completed.status.value, payload=as_json(completed))
            )
            await connection.execute(
                action_dispatch_attempts.insert().values(
                    attempt_id=attempt.attempt_id,
                    action_dispatch_id=attempt.action_dispatch_id,
                    payload=as_json(attempt),
                )
            )
        return completed

    async def record_action_dispatch_attempt(
        self, attempt: ActionDispatchAttempt
    ) -> ActionDispatchAttempt:
        async with self.engine.begin() as connection:
            await connection.execute(
                action_dispatch_attempts.insert().values(
                    attempt_id=attempt.attempt_id,
                    action_dispatch_id=attempt.action_dispatch_id,
                    payload=as_json(attempt),
                )
            )
        return attempt

    async def list_action_dispatch_attempts(
        self, tenant_id: str, action_dispatch_id: UUID
    ) -> list[ActionDispatchAttempt]:
        async with self.engine.connect() as connection:
            action_payload = (
                await connection.execute(
                    select(action_dispatches.c.payload).where(
                        action_dispatches.c.tenant_id == tenant_id,
                        action_dispatches.c.action_dispatch_id == action_dispatch_id,
                    )
                )
            ).scalar_one_or_none()
            if not action_payload:
                return []
            rows = (
                (
                    await connection.execute(
                        select(action_dispatch_attempts.c.payload).where(
                            action_dispatch_attempts.c.action_dispatch_id == action_dispatch_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [ActionDispatchAttempt.model_validate(row) for row in rows]

    async def record_memory_audit(self, event: MemoryAuditEvent) -> MemoryAuditEvent:
        event = event.model_copy(update={"details": redact(event.details)})
        async with self.engine.begin() as connection:
            await connection.execute(
                memory_audit_events.insert().values(
                    audit_id=event.audit_id,
                    tenant_id=event.tenant_id,
                    traveler_id=event.traveler_id,
                    payload=as_json(event),
                )
            )
        return event

    async def list_memory_audit(
        self, tenant_id: str, traveler_id: str | None = None
    ) -> list[MemoryAuditEvent]:
        statement = select(memory_audit_events.c.payload).where(
            memory_audit_events.c.tenant_id == tenant_id
        )
        if traveler_id:
            statement = statement.where(memory_audit_events.c.traveler_id == traveler_id)
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).scalars().all()
        return [MemoryAuditEvent.model_validate(row) for row in rows]

    async def save_change_record(self, record: ChangeRecord) -> ChangeRecord:
        async with self.engine.begin() as connection:
            await connection.execute(
                change_records.insert().values(
                    change_id=record.change_id,
                    tenant_id=record.tenant_id,
                    change_type=record.change_type.value,
                    payload=as_json(record),
                )
            )
        return record

    async def list_change_records(self, tenant_id: str) -> list[ChangeRecord]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(change_records.c.payload).where(change_records.c.tenant_id == tenant_id)
                )
            ).scalars().all()
        return [ChangeRecord.model_validate(row) for row in rows]

    async def save_provider_onboarding(
        self, record: ProviderOnboardingRecord
    ) -> ProviderOnboardingRecord:
        async with self.engine.begin() as connection:
            await connection.execute(
                provider_onboarding_records.insert().values(
                    provider_onboarding_id=record.provider_onboarding_id,
                    tenant_id=record.tenant_id,
                    status=record.status.value,
                    payload=as_json(record),
                )
            )
        return record

    async def list_provider_onboarding(self, tenant_id: str) -> list[ProviderOnboardingRecord]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(provider_onboarding_records.c.payload).where(
                        provider_onboarding_records.c.tenant_id == tenant_id
                    )
                )
            ).scalars().all()
        return [ProviderOnboardingRecord.model_validate(row) for row in rows]

    async def get_runtime_control_override(
        self, tenant_id: str, control_name: RuntimeControlName
    ) -> RuntimeControlChange | None:
        async with self.engine.connect() as connection:
            payload = (
                await connection.execute(
                    select(runtime_control_overrides.c.payload).where(
                        runtime_control_overrides.c.tenant_id == tenant_id,
                        runtime_control_overrides.c.control_name == control_name.value,
                    )
                )
            ).scalar_one_or_none()
        record = RuntimeControlChange.model_validate(payload) if payload else None
        if record and record.expires_at and record.expires_at <= datetime.now(UTC):
            return None
        return record

    async def save_runtime_control_change(
        self, record: RuntimeControlChange
    ) -> RuntimeControlChange:
        async with self.engine.begin() as connection:
            await connection.execute(
                runtime_control_changes.insert().values(
                    control_change_id=record.control_change_id,
                    tenant_id=record.tenant_id,
                    control_name=record.control_name.value,
                    payload=as_json(record),
                )
            )
            await connection.execute(
                pg_insert(runtime_control_overrides)
                .values(
                    tenant_id=record.tenant_id,
                    control_name=record.control_name.value,
                    payload=as_json(record),
                )
                .on_conflict_do_update(
                    index_elements=[
                        runtime_control_overrides.c.tenant_id,
                        runtime_control_overrides.c.control_name,
                    ],
                    set_={"payload": as_json(record)},
                )
            )
        return record

    async def list_runtime_control_changes(self, tenant_id: str) -> list[RuntimeControlChange]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(runtime_control_changes.c.payload)
                    .where(runtime_control_changes.c.tenant_id == tenant_id)
                    .order_by(runtime_control_changes.c.control_change_id)
                )
            ).scalars().all()
        return [RuntimeControlChange.model_validate(row) for row in rows]

    async def get_platform_control_override(
        self, control_name: RuntimeControlName
    ) -> PlatformRuntimeControlChange | None:
        async with self.engine.connect() as connection:
            payload = (
                await connection.execute(
                    select(platform_runtime_control_overrides.c.payload).where(
                        platform_runtime_control_overrides.c.control_name == control_name.value
                    )
                )
            ).scalar_one_or_none()
        record = PlatformRuntimeControlChange.model_validate(payload) if payload else None
        if record and record.expires_at and record.expires_at <= datetime.now(UTC):
            return None
        return record

    async def save_platform_control_change(
        self, record: PlatformRuntimeControlChange
    ) -> PlatformRuntimeControlChange:
        async with self.engine.begin() as connection:
            await connection.execute(
                platform_runtime_control_changes.insert().values(
                    control_change_id=record.control_change_id,
                    control_name=record.control_name.value,
                    payload=as_json(record),
                )
            )
            await connection.execute(
                pg_insert(platform_runtime_control_overrides)
                .values(control_name=record.control_name.value, payload=as_json(record))
                .on_conflict_do_update(
                    index_elements=[platform_runtime_control_overrides.c.control_name],
                    set_={"payload": as_json(record)},
                )
            )
        return record

    async def list_platform_control_changes(self) -> list[PlatformRuntimeControlChange]:
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(platform_runtime_control_changes.c.payload).order_by(
                            platform_runtime_control_changes.c.control_change_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [PlatformRuntimeControlChange.model_validate(row) for row in rows]

    async def save_original_upload(self, record: OriginalUploadRecord) -> OriginalUploadRecord:
        async with self.engine.begin() as connection:
            await connection.execute(
                pg_insert(original_uploads)
                .values(
                    original_upload_id=record.original_upload_id,
                    tenant_id=record.tenant_id,
                    status=record.status.value,
                    payload=as_json(record),
                )
                .on_conflict_do_update(
                    index_elements=[original_uploads.c.original_upload_id],
                    set_={"status": record.status.value, "payload": as_json(record)},
                )
            )
        return record

    async def get_original_upload(
        self, tenant_id: str, original_upload_id: UUID
    ) -> OriginalUploadRecord | None:
        async with self.engine.connect() as connection:
            payload = (
                await connection.execute(
                    select(original_uploads.c.payload).where(
                        original_uploads.c.tenant_id == tenant_id,
                        original_uploads.c.original_upload_id == original_upload_id,
                    )
                )
            ).scalar_one_or_none()
        return OriginalUploadRecord.model_validate(payload) if payload else None

    async def save_legal_hold(self, hold: LegalHoldRecord) -> LegalHoldRecord:
        status = "released" if hold.released_at else "active"
        async with self.engine.begin() as connection:
            await connection.execute(
                pg_insert(legal_holds)
                .values(
                    legal_hold_id=hold.legal_hold_id,
                    tenant_id=hold.tenant_id,
                    status=status,
                    payload=as_json(hold),
                )
                .on_conflict_do_update(
                    index_elements=[legal_holds.c.legal_hold_id],
                    set_={"status": status, "payload": as_json(hold)},
                )
            )
        return hold

    async def get_legal_hold(self, tenant_id: str, legal_hold_id: UUID) -> LegalHoldRecord | None:
        async with self.engine.connect() as connection:
            payload = (
                await connection.execute(
                    select(legal_holds.c.payload).where(
                        legal_holds.c.tenant_id == tenant_id,
                        legal_holds.c.legal_hold_id == legal_hold_id,
                    )
                )
            ).scalar_one_or_none()
        return LegalHoldRecord.model_validate(payload) if payload else None

    async def list_legal_holds(self, tenant_id: str) -> list[LegalHoldRecord]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(legal_holds.c.payload)
                    .where(legal_holds.c.tenant_id == tenant_id)
                    .order_by(legal_holds.c.legal_hold_id)
                )
            ).scalars().all()
        return [LegalHoldRecord.model_validate(row) for row in rows]

    async def active_legal_holds_for_traveler(
        self, tenant_id: str, traveler_id: str, *, now: datetime | None = None
    ) -> list[LegalHoldRecord]:
        timestamp = now or datetime.now(UTC)
        async with self.engine.connect() as connection:
            trip_rows = (
                await connection.execute(select(trips.c.trip_id, trips.c.payload))
            ).all()
        trip_ids = {
            row.trip_id
            for row in trip_rows
            if Trip.model_validate(row.payload).tenant_id == tenant_id
            and Trip.model_validate(row.payload).traveler_id == traveler_id
        }
        return [
            hold
            for hold in await self.list_legal_holds(tenant_id)
            if hold.is_active(timestamp)
            and (
                hold.scope == LegalHoldScope.TENANT
                or (hold.scope == LegalHoldScope.TRAVELER and hold.traveler_id == traveler_id)
                or (hold.scope == LegalHoldScope.TRIP and hold.trip_id in trip_ids)
            )
        ]

    async def save_deletion_request(self, request: DeletionRequest) -> DeletionRequest:
        async with self.engine.begin() as connection:
            await connection.execute(
                pg_insert(deletion_requests)
                .values(
                    deletion_request_id=request.deletion_request_id,
                    tenant_id=request.tenant_id,
                    traveler_id=request.traveler_id,
                    status=request.status.value,
                    payload=as_json(request),
                )
                .on_conflict_do_update(
                    index_elements=[deletion_requests.c.deletion_request_id],
                    set_={"status": request.status.value, "payload": as_json(request)},
                )
            )
        return request

    async def get_deletion_request(
        self, tenant_id: str, deletion_request_id: UUID
    ) -> DeletionRequest | None:
        async with self.engine.connect() as connection:
            payload = (
                await connection.execute(
                    select(deletion_requests.c.payload).where(
                        deletion_requests.c.tenant_id == tenant_id,
                        deletion_requests.c.deletion_request_id == deletion_request_id,
                    )
                )
            ).scalar_one_or_none()
        return DeletionRequest.model_validate(payload) if payload else None

    async def list_deletion_requests(
        self, tenant_id: str, traveler_id: str | None = None
    ) -> list[DeletionRequest]:
        statement = select(deletion_requests.c.payload).where(
            deletion_requests.c.tenant_id == tenant_id
        )
        if traveler_id:
            statement = statement.where(deletion_requests.c.traveler_id == traveler_id)
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    statement.order_by(deletion_requests.c.deletion_request_id)
                )
            ).scalars().all()
        return [DeletionRequest.model_validate(row) for row in rows]

    async def list_pending_deletion_requests(self) -> list[DeletionRequest]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(deletion_requests.c.payload)
                    .where(
                        deletion_requests.c.status.in_(
                            [
                                DeletionRequestStatus.PENDING.value,
                                DeletionRequestStatus.BLOCKED_BY_LEGAL_HOLD.value,
                            ]
                        )
                    )
                    .order_by(deletion_requests.c.deletion_request_id)
                )
            ).scalars().all()
        return [DeletionRequest.model_validate(row) for row in rows]

    async def erase_traveler_data(
        self,
        tenant_id: str,
        traveler_id: str,
        *,
        scope: DeletionRequestScope = DeletionRequestScope.TRAVELER_DATA,
    ) -> dict[str, int]:
        """Erase a traveler's mutable operational data in one transaction.

        Legal-hold eligibility is intentionally checked by the worker before
        this method begins.  Repeating an already completed request returns
        zero counts, making redelivery safe.
        """
        async with self.engine.begin() as connection:
            profile_present = (
                await connection.execute(
                    select(preferences.c.traveler_id).where(
                        preferences.c.tenant_id == tenant_id,
                        preferences.c.traveler_id == traveler_id,
                    )
                )
            ).scalar_one_or_none()
            proposal_rows = (
                await connection.execute(
                    select(memory_proposals.c.proposal_id, memory_proposals.c.payload).where(
                        memory_proposals.c.tenant_id == tenant_id
                    )
                )
            ).all()
            proposal_ids = [
                row.proposal_id
                for row in proposal_rows
                if MemoryUpdateProposal.model_validate(row.payload).traveler_id == traveler_id
            ]
            await connection.execute(
                preferences.delete().where(
                    preferences.c.tenant_id == tenant_id,
                    preferences.c.traveler_id == traveler_id,
                )
            )
            if proposal_ids:
                await connection.execute(
                    memory_proposals.delete().where(memory_proposals.c.proposal_id.in_(proposal_ids))
                )
            counts = {
                "profiles": int(profile_present is not None),
                "memory_proposals": len(proposal_ids),
            }
            if scope == DeletionRequestScope.PREFERENCE_MEMORY:
                return counts

            trip_rows = (
                await connection.execute(select(trips.c.trip_id, trips.c.payload))
            ).all()
            trip_ids = [
                row.trip_id
                for row in trip_rows
                if Trip.model_validate(row.payload).tenant_id == tenant_id
                and Trip.model_validate(row.payload).traveler_id == traveler_id
            ]
            incident_rows = (
                await connection.execute(select(incidents.c.incident_id, incidents.c.payload))
            ).all()
            incident_ids = [
                row.incident_id
                for row in incident_rows
                if Incident.model_validate(row.payload).tenant_id == tenant_id
                and Incident.model_validate(row.payload).trip_id in trip_ids
            ]
            notification_rows = (
                await connection.execute(
                    select(notifications.c.notification_id, notifications.c.payload).where(
                        notifications.c.tenant_id == tenant_id
                    )
                )
            ).all()
            notification_ids = [
                row.notification_id
                for row in notification_rows
                if NotificationRecord.model_validate(row.payload).traveler_id == traveler_id
            ]
            if notification_ids:
                await connection.execute(
                    notification_attempts.delete().where(
                        notification_attempts.c.notification_id.in_(notification_ids)
                    )
                )
                await connection.execute(
                    notifications.delete().where(notifications.c.notification_id.in_(notification_ids))
                )
            if incident_ids:
                await connection.execute(
                    action_dispatch_attempts.delete().where(
                        action_dispatch_attempts.c.action_dispatch_id.in_(
                            select(action_dispatches.c.action_dispatch_id).where(
                                action_dispatches.c.incident_id.in_(incident_ids)
                            )
                        )
                    )
                )
                await connection.execute(
                    action_dispatches.delete().where(action_dispatches.c.incident_id.in_(incident_ids))
                )
                await connection.execute(
                    approvals.delete().where(approvals.c.incident_id.in_(incident_ids))
                )
                await connection.execute(
                    candidate_sets.delete().where(candidate_sets.c.incident_id.in_(incident_ids))
                )
                await connection.execute(
                    candidate_outcomes.delete().where(
                        candidate_outcomes.c.incident_id.in_(incident_ids)
                    )
                )
                await connection.execute(
                    manager_feedback.delete().where(manager_feedback.c.incident_id.in_(incident_ids))
                )
                await connection.execute(
                    incidents.delete().where(incidents.c.incident_id.in_(incident_ids))
                )
            if trip_ids:
                await connection.execute(
                    evidence_items.delete().where(evidence_items.c.trip_id.in_(trip_ids))
                )
                await connection.execute(
                    assessments.delete().where(assessments.c.trip_id.in_(trip_ids))
                )
                await connection.execute(
                    trip_segments.delete().where(trip_segments.c.trip_id.in_(trip_ids))
                )
                await connection.execute(trips.delete().where(trips.c.trip_id.in_(trip_ids)))
            counts.update(
                {
                    "trips": len(trip_ids),
                    "evidence": len(trip_ids),
                    "assessments": len(trip_ids),
                    "incidents": len(incident_ids),
                    "notifications": len(notification_ids),
                }
            )
            return counts

    async def purge_expired(
        self,
        before: datetime,
        *,
        original_upload_before: datetime | None = None,
        audit_before: datetime | None = None,
    ) -> dict[str, int]:
        """Delete rows older than the policy cutoff using model timestamps.

        Timestamps currently live in the immutable JSON document, so parsing the
        relatively small candidate set in Python keeps this compatible with the
        existing schema and avoids database-specific JSON timestamp casts.
        """
        original_upload_cutoff = original_upload_before or before
        audit_cutoff = audit_before or before
        async with self.engine.begin() as connection:
            trip_rows = (
                await connection.execute(select(trips.c.trip_id, trips.c.payload))
            ).all()
            active_holds = [
                LegalHoldRecord.model_validate(row)
                for row in (
                    await connection.execute(select(legal_holds.c.payload))
                ).scalars().all()
                if LegalHoldRecord.model_validate(row).is_active()
            ]
            parsed_trips = {
                row.trip_id: Trip.model_validate(row.payload) for row in trip_rows
            }
            held_trip_ids = {
                trip_id
                for trip_id, trip in parsed_trips.items()
                if any(
                    hold.tenant_id == trip.tenant_id
                    and (
                        hold.scope == LegalHoldScope.TENANT
                        or (
                            hold.scope == LegalHoldScope.TRAVELER
                            and hold.traveler_id == trip.traveler_id
                        )
                        or (hold.scope == LegalHoldScope.TRIP and hold.trip_id == trip_id)
                    )
                    for hold in active_holds
                )
            }
            held_travelers = {
                (hold.tenant_id, hold.traveler_id)
                for hold in active_holds
                if hold.scope == LegalHoldScope.TRAVELER and hold.traveler_id
            }
            held_tenants = {
                hold.tenant_id for hold in active_holds if hold.scope == LegalHoldScope.TENANT
            }
            expired_trip_ids = [
                row.trip_id
                for row in trip_rows
                if parsed_trips[row.trip_id].created_at < before
                and row.trip_id not in held_trip_ids
            ]
            incident_rows = (
                await connection.execute(select(incidents.c.incident_id, incidents.c.payload))
            ).all()
            expired_incident_ids = [
                row.incident_id
                for row in incident_rows
                if Incident.model_validate(row.payload).trip_id in expired_trip_ids
            ]
            profile_rows = (
                await connection.execute(
                    select(
                        preferences.c.tenant_id,
                        preferences.c.traveler_id,
                        preferences.c.payload,
                    )
                )
            ).all()
            expired_profiles = [
                (row.tenant_id, row.traveler_id)
                for row in profile_rows
                if TravelerPreferenceProfile.model_validate(row.payload).updated_at < before
                and (row.tenant_id, row.traveler_id) not in held_travelers
                and row.tenant_id not in held_tenants
            ]
            proposal_rows = (
                await connection.execute(
                    select(memory_proposals.c.proposal_id, memory_proposals.c.payload)
                )
            ).all()
            expired_proposal_ids = [
                row.proposal_id
                for row in proposal_rows
                if MemoryUpdateProposal.model_validate(row.payload).created_at < before
                and (
                    MemoryUpdateProposal.model_validate(row.payload).tenant_id,
                    MemoryUpdateProposal.model_validate(row.payload).traveler_id,
                )
                not in held_travelers
                and MemoryUpdateProposal.model_validate(row.payload).tenant_id not in held_tenants
            ]
            notification_rows = (
                await connection.execute(
                    select(notifications.c.notification_id, notifications.c.payload)
                )
            ).all()
            expired_notification_ids = [
                row.notification_id
                for row in notification_rows
                if NotificationRecord.model_validate(row.payload).created_at < before
            ]
            upload_rows = (
                await connection.execute(
                    select(
                        original_uploads.c.original_upload_id,
                        original_uploads.c.tenant_id,
                        original_uploads.c.payload,
                    )
                )
            ).all()
            expired_upload_ids = [
                row.original_upload_id
                for row in upload_rows
                if OriginalUploadRecord.model_validate(row.payload).created_at
                < original_upload_cutoff
                and row.tenant_id not in held_tenants
            ]
            event_rows = (
                await connection.execute(select(events.c.event_id, events.c.payload))
            ).all()
            expired_event_ids = [
                row.event_id
                for row in event_rows
                if EventRecord.model_validate(row.payload).emitted_at < audit_cutoff
            ]
            memory_audit_rows = (
                await connection.execute(
                    select(memory_audit_events.c.audit_id, memory_audit_events.c.payload)
                )
            ).all()
            expired_memory_audit_ids = [
                row.audit_id
                for row in memory_audit_rows
                if MemoryAuditEvent.model_validate(row.payload).occurred_at < audit_cutoff
            ]
            if expired_trip_ids:
                await connection.execute(
                    evidence_items.delete().where(evidence_items.c.trip_id.in_(expired_trip_ids))
                )
                await connection.execute(
                    assessments.delete().where(assessments.c.trip_id.in_(expired_trip_ids))
                )
            if expired_incident_ids:
                await connection.execute(
                    approvals.delete().where(approvals.c.incident_id.in_(expired_incident_ids))
                )
                await connection.execute(
                    candidate_sets.delete().where(candidate_sets.c.incident_id.in_(expired_incident_ids))
                )
                await connection.execute(
                    candidate_outcomes.delete().where(
                        candidate_outcomes.c.incident_id.in_(expired_incident_ids)
                    )
                )
                await connection.execute(
                    manager_feedback.delete().where(
                        manager_feedback.c.incident_id.in_(expired_incident_ids)
                    )
                )
                await connection.execute(
                    action_dispatch_attempts.delete().where(
                        action_dispatch_attempts.c.action_dispatch_id.in_(
                            select(action_dispatches.c.action_dispatch_id).where(
                                action_dispatches.c.incident_id.in_(expired_incident_ids)
                            )
                        )
                    )
                )
                await connection.execute(
                    action_dispatches.delete().where(
                        action_dispatches.c.incident_id.in_(expired_incident_ids)
                    )
                )
                await connection.execute(
                    incidents.delete().where(incidents.c.incident_id.in_(expired_incident_ids))
                )
            if expired_trip_ids:
                await connection.execute(
                    trips.delete().where(trips.c.trip_id.in_(expired_trip_ids))
                )
            for tenant_id, traveler_id in expired_profiles:
                await connection.execute(
                    preferences.delete().where(
                        preferences.c.tenant_id == tenant_id,
                        preferences.c.traveler_id == traveler_id,
                    )
                )
            if expired_proposal_ids:
                await connection.execute(
                    memory_proposals.delete().where(memory_proposals.c.proposal_id.in_(expired_proposal_ids))
                )
            if expired_notification_ids:
                await connection.execute(
                    notification_attempts.delete().where(
                        notification_attempts.c.notification_id.in_(expired_notification_ids)
                    )
                )
                await connection.execute(
                    notifications.delete().where(notifications.c.notification_id.in_(expired_notification_ids))
                )
            if expired_upload_ids:
                await connection.execute(
                    original_uploads.delete().where(
                        original_uploads.c.original_upload_id.in_(expired_upload_ids)
                    )
                )
            if expired_event_ids:
                await connection.execute(
                    events.delete().where(events.c.event_id.in_(expired_event_ids))
                )
            if expired_memory_audit_ids:
                await connection.execute(
                    memory_audit_events.delete().where(
                        memory_audit_events.c.audit_id.in_(expired_memory_audit_ids)
                    )
                )
        return {
            "trips": len(expired_trip_ids),
            "evidence": len(expired_trip_ids),
            "assessments": len(expired_trip_ids),
            "incidents": len(expired_incident_ids),
            "profiles": len(expired_profiles),
            "memory_proposals": len(expired_proposal_ids),
            "notifications": len(expired_notification_ids),
            "original_uploads": len(expired_upload_ids),
            "audit_events": len(expired_event_ids),
            "memory_audits": len(expired_memory_audit_ids),
        }
