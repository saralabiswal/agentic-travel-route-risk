"""MVP repository seam. Replace this implementation with tenant-filtered SQLAlchemy queries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

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
    FlightSegmentCreate,
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


class InMemoryRouteShieldRepository:
    def __init__(self) -> None:
        self.trips: dict[UUID, Trip] = {}
        self.trip_segments: dict[UUID, list[FlightSegmentCreate]] = {}
        self.evidence: dict[UUID, list[EvidenceEnvelope]] = {}
        self.assessments: dict[UUID, list[RiskAssessment]] = {}
        self.incidents: dict[UUID, Incident] = {}
        self.approvals: dict[UUID, ApprovalRecord] = {}
        self.events: list[EventRecord] = []
        self.profiles: dict[tuple[str, str], TravelerPreferenceProfile] = {}
        self.memory_proposals: dict[UUID, MemoryUpdateProposal] = {}
        self.idempotency: dict[tuple[str, str], ApprovalRecord] = {}
        self.idempotency_records: dict[tuple[str, str], IdempotencyRecord] = {}
        self._idempotency_lock = asyncio.Lock()
        self.candidate_sets: dict[UUID, tuple[str, RecoveryCandidateSet]] = {}
        self.candidate_outcomes: dict[UUID, tuple[str, RecoveryCandidateOutcome]] = {}
        self.manager_feedback: dict[UUID, ManagerFeedback] = {}
        self.tenant_playbooks: dict[UUID, TenantPlaybook] = {}
        self.notifications: dict[UUID, NotificationRecord] = {}
        self.notification_attempts: dict[UUID, list[NotificationAttempt]] = {}
        self._notification_lock = asyncio.Lock()
        self.action_dispatches: dict[UUID, ActionDispatchRecord] = {}
        self.action_dispatch_attempts: dict[UUID, list[ActionDispatchAttempt]] = {}
        self._action_dispatch_lock = asyncio.Lock()
        self.memory_audits: list[MemoryAuditEvent] = []
        self.change_records: dict[UUID, ChangeRecord] = {}
        self.provider_onboarding_records: dict[UUID, ProviderOnboardingRecord] = {}
        self.legal_holds: dict[UUID, LegalHoldRecord] = {}
        self.deletion_requests: dict[UUID, DeletionRequest] = {}
        self.runtime_control_overrides: dict[
            tuple[str, RuntimeControlName], RuntimeControlChange
        ] = {}
        self.runtime_control_changes: list[RuntimeControlChange] = []
        self.platform_control_overrides: dict[RuntimeControlName, PlatformRuntimeControlChange] = {}
        self.platform_control_changes: list[PlatformRuntimeControlChange] = []
        self.original_uploads: dict[UUID, OriginalUploadRecord] = {}

    async def create_trip(self, trip: Trip) -> Trip:
        self.trips[trip.trip_id] = trip
        self.trip_segments[trip.trip_id] = list(trip.segments)
        return trip

    async def save_trip(self, trip: Trip) -> Trip:
        self.trips[trip.trip_id] = trip
        self.trip_segments[trip.trip_id] = list(trip.segments)
        return trip

    async def get_trip(self, tenant_id: str, trip_id: UUID) -> Trip | None:
        trip = self.trips.get(trip_id)
        return trip if trip and trip.tenant_id == tenant_id else None

    async def list_trips(self) -> list[Trip]:
        return list(self.trips.values())

    async def save_evidence(
        self, tenant_id: str, trip_id: UUID, evidence: list[EvidenceEnvelope]
    ) -> None:
        if not await self.get_trip(tenant_id, trip_id):
            raise KeyError("trip not found")
        self.evidence[trip_id] = evidence

    async def get_evidence(self, tenant_id: str, trip_id: UUID) -> list[EvidenceEnvelope]:
        if not await self.get_trip(tenant_id, trip_id):
            return []
        return self.evidence.get(trip_id, [])

    async def save_assessment(self, tenant_id: str, assessment: RiskAssessment) -> RiskAssessment:
        if not await self.get_trip(tenant_id, assessment.trip_id):
            raise KeyError("trip not found")
        self.assessments.setdefault(assessment.trip_id, []).append(assessment)
        return assessment

    async def save_incident(self, incident: Incident) -> Incident:
        self.incidents[incident.incident_id] = incident
        return incident

    async def get_incident(self, tenant_id: str, incident_id: UUID) -> Incident | None:
        incident = self.incidents.get(incident_id)
        return incident if incident and incident.tenant_id == tenant_id else None

    async def list_incidents(self, tenant_id: str) -> list[Incident]:
        return [incident for incident in self.incidents.values() if incident.tenant_id == tenant_id]

    async def save_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        self.approvals[approval.incident_id] = approval
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
        """Claim a request once, or report a stable replay/conflict state."""
        key = (tenant_id, idempotency_key)
        async with self._idempotency_lock:
            existing = self.idempotency_records.get(key)
            now = datetime.now(UTC)
            if existing and existing.expires_at <= now:
                existing = None
                self.idempotency_records.pop(key, None)
            if not existing:
                record = IdempotencyRecord(
                    tenant_id=tenant_id,
                    idempotency_key=idempotency_key,
                    scope=scope,
                    request_hash=request_hash,
                    expires_at=expires_at,
                )
                self.idempotency_records[key] = record
                return "execute", record
            if existing.scope != scope or existing.request_hash != request_hash:
                return "conflict", existing
            if existing.state == IdempotencyState.COMPLETED:
                return "replay", existing
            return "in_progress", existing

    async def complete_idempotency(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        request_hash: str,
        response_status_code: int,
        response_payload: dict[str, object],
    ) -> IdempotencyRecord:
        key = (tenant_id, idempotency_key)
        async with self._idempotency_lock:
            record = self.idempotency_records.get(key)
            if not record or record.request_hash != request_hash:
                raise KeyError("idempotency claim not found")
            record.state = IdempotencyState.COMPLETED
            record.response_status_code = response_status_code
            record.response_payload = response_payload
            record.completed_at = datetime.now(UTC)
            return record

    async def record_event(self, event: EventRecord) -> EventRecord:
        safe_event = event.model_copy(update={"details": redact(event.details)})
        self.events.append(safe_event)
        return safe_event

    async def list_events(self, tenant_id: str) -> list[EventRecord]:
        return [event for event in self.events if event.tenant_id == tenant_id]

    async def save_candidate_set(
        self, tenant_id: str, candidate_set: RecoveryCandidateSet
    ) -> RecoveryCandidateSet:
        self.candidate_sets[candidate_set.candidate_set_id] = (tenant_id, candidate_set)
        return candidate_set

    async def get_candidate_set(
        self, tenant_id: str, incident_id: UUID, candidate_set_id: UUID
    ) -> RecoveryCandidateSet | None:
        saved = self.candidate_sets.get(candidate_set_id)
        if not saved:
            return None
        saved_tenant, candidate_set = saved
        if saved_tenant != tenant_id or candidate_set.incident_id != incident_id:
            return None
        return candidate_set

    async def list_candidate_sets(
        self, tenant_id: str, incident_id: UUID
    ) -> list[RecoveryCandidateSet]:
        return [
            item
            for saved_tenant, item in self.candidate_sets.values()
            if saved_tenant == tenant_id and item.incident_id == incident_id
        ]

    async def record_candidate_outcome(
        self, tenant_id: str, outcome: RecoveryCandidateOutcome
    ) -> RecoveryCandidateOutcome:
        self.candidate_outcomes[outcome.outcome_id] = (tenant_id, outcome)
        return outcome

    async def list_candidate_outcomes(
        self, tenant_id: str, candidate_set_id: UUID
    ) -> list[RecoveryCandidateOutcome]:
        return sorted(
            (
                outcome
                for saved_tenant, outcome in self.candidate_outcomes.values()
                if saved_tenant == tenant_id and outcome.candidate_set_id == candidate_set_id
            ),
            key=lambda outcome: (outcome.occurred_at, str(outcome.outcome_id)),
        )

    async def save_manager_feedback(self, feedback: ManagerFeedback) -> ManagerFeedback:
        self.manager_feedback[feedback.feedback_id] = feedback
        return feedback

    async def list_manager_feedback(
        self, tenant_id: str, incident_id: UUID
    ) -> list[ManagerFeedback]:
        return sorted(
            (
                feedback
                for feedback in self.manager_feedback.values()
                if feedback.tenant_id == tenant_id and feedback.incident_id == incident_id
            ),
            key=lambda feedback: (feedback.created_at, str(feedback.feedback_id)),
        )

    async def get_playbook(self, tenant_id: str, playbook_id: UUID) -> TenantPlaybook | None:
        playbook = self.tenant_playbooks.get(playbook_id)
        return playbook if playbook and playbook.tenant_id == tenant_id else None

    async def save_playbook(self, playbook: TenantPlaybook) -> TenantPlaybook:
        existing = self.tenant_playbooks.get(playbook.playbook_id)
        if existing:
            return existing
        self.tenant_playbooks[playbook.playbook_id] = playbook
        return playbook

    async def list_approved_playbooks(self, tenant_id: str) -> list[TenantPlaybook]:
        return sorted(
            (
                playbook
                for playbook in self.tenant_playbooks.values()
                if playbook.tenant_id == tenant_id
            ),
            key=lambda playbook: (playbook.name, playbook.version),
        )

    async def get_profile(
        self, tenant_id: str, traveler_id: str
    ) -> TravelerPreferenceProfile | None:
        return self.profiles.get((tenant_id, traveler_id))

    async def save_profile(self, profile: TravelerPreferenceProfile) -> TravelerPreferenceProfile:
        self.profiles[(profile.tenant_id, profile.traveler_id)] = profile
        return profile

    async def save_memory_proposal(self, proposal: MemoryUpdateProposal) -> MemoryUpdateProposal:
        self.memory_proposals[proposal.proposal_id] = proposal
        return proposal

    async def get_memory_proposal(
        self, tenant_id: str, proposal_id: UUID
    ) -> MemoryUpdateProposal | None:
        proposal = self.memory_proposals.get(proposal_id)
        return proposal if proposal and proposal.tenant_id == tenant_id else None

    async def delete_traveler_memory(self, tenant_id: str, traveler_id: str) -> dict[str, int]:
        deleted_profile = int(self.profiles.pop((tenant_id, traveler_id), None) is not None)
        proposal_ids = [
            proposal_id
            for proposal_id, proposal in self.memory_proposals.items()
            if proposal.tenant_id == tenant_id and proposal.traveler_id == traveler_id
        ]
        for proposal_id in proposal_ids:
            self.memory_proposals.pop(proposal_id, None)
        return {"profiles": deleted_profile, "memory_proposals": len(proposal_ids)}

    async def save_notification(self, notification: NotificationRecord) -> NotificationRecord:
        self.notifications[notification.notification_id] = notification
        return notification

    async def get_notification(
        self, tenant_id: str, notification_id: UUID
    ) -> NotificationRecord | None:
        notification = self.notifications.get(notification_id)
        return notification if notification and notification.tenant_id == tenant_id else None

    async def list_notifications(
        self, tenant_id: str, statuses: set[NotificationStatus] | None = None
    ) -> list[NotificationRecord]:
        return [
            notification
            for notification in self.notifications.values()
            if notification.tenant_id == tenant_id
            and (statuses is None or notification.status in statuses)
        ]

    async def list_due_notifications(self, now: datetime | None = None) -> list[NotificationRecord]:
        timestamp = now or datetime.now(UTC)
        return [
            notification
            for notification in self.notifications.values()
            if notification.status
            in {NotificationStatus.QUEUED, NotificationStatus.RETRY_SCHEDULED}
            and notification.next_attempt_at <= timestamp
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
        """Atomically lease due outbox records to one notification worker."""
        timestamp = now or datetime.now(UTC)
        claimed: list[NotificationRecord] = []
        async with self._notification_lock:
            for notification_id, notification in self.notifications.items():
                if len(claimed) >= limit:
                    break
                if tenant_id and notification.tenant_id != tenant_id:
                    continue
                if notification.status not in {
                    NotificationStatus.QUEUED,
                    NotificationStatus.RETRY_SCHEDULED,
                } or notification.next_attempt_at > timestamp:
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
                self.notifications[notification_id] = leased
                claimed.append(leased.model_copy())
        return claimed

    async def complete_notification_delivery(
        self,
        *,
        notification: NotificationRecord,
        attempt: NotificationAttempt,
        claim_id: UUID,
    ) -> NotificationRecord | None:
        """Commit one leased delivery and its attempt record as a single transition."""
        async with self._notification_lock:
            current = self.notifications.get(notification.notification_id)
            if not current or current.dispatch_claim_id != claim_id:
                return None
            completed = notification.model_copy(
                update={
                    "dispatch_claim_id": None,
                    "dispatch_claimed_by": None,
                    "dispatch_claimed_at": None,
                    "dispatch_claim_expires_at": None,
                }
            )
            self.notifications[completed.notification_id] = completed
            self.notification_attempts.setdefault(attempt.notification_id, []).append(attempt)
            return completed

    async def record_notification_attempt(
        self, attempt: NotificationAttempt
    ) -> NotificationAttempt:
        self.notification_attempts.setdefault(attempt.notification_id, []).append(attempt)
        return attempt

    async def list_notification_attempts(
        self, tenant_id: str, notification_id: UUID
    ) -> list[NotificationAttempt]:
        if not await self.get_notification(tenant_id, notification_id):
            return []
        return self.notification_attempts.get(notification_id, [])

    async def save_action_dispatch(self, action: ActionDispatchRecord) -> ActionDispatchRecord:
        self.action_dispatches[action.action_dispatch_id] = action
        return action

    async def list_action_dispatches(
        self, tenant_id: str, incident_id: UUID
    ) -> list[ActionDispatchRecord]:
        return [
            action
            for action in self.action_dispatches.values()
            if action.tenant_id == tenant_id and action.incident_id == incident_id
        ]

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
        claimed: list[ActionDispatchRecord] = []
        async with self._action_dispatch_lock:
            for action_id, action in self.action_dispatches.items():
                if len(claimed) >= limit:
                    break
                if tenant_id and action.tenant_id != tenant_id:
                    continue
                if action.status not in {
                    ActionDispatchStatus.QUEUED,
                    ActionDispatchStatus.RETRY_SCHEDULED,
                } or action.next_attempt_at > timestamp:
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
                self.action_dispatches[action_id] = leased
                claimed.append(leased.model_copy())
        return claimed

    async def complete_action_dispatch(
        self,
        *,
        action: ActionDispatchRecord,
        attempt: ActionDispatchAttempt,
        claim_id: UUID,
    ) -> ActionDispatchRecord | None:
        async with self._action_dispatch_lock:
            current = self.action_dispatches.get(action.action_dispatch_id)
            if not current or current.dispatch_claim_id != claim_id:
                return None
            completed = action.model_copy(
                update={
                    "dispatch_claim_id": None,
                    "dispatch_claimed_by": None,
                    "dispatch_claimed_at": None,
                    "dispatch_claim_expires_at": None,
                }
            )
            self.action_dispatches[completed.action_dispatch_id] = completed
            self.action_dispatch_attempts.setdefault(attempt.action_dispatch_id, []).append(attempt)
            return completed

    async def record_action_dispatch_attempt(
        self, attempt: ActionDispatchAttempt
    ) -> ActionDispatchAttempt:
        self.action_dispatch_attempts.setdefault(attempt.action_dispatch_id, []).append(attempt)
        return attempt

    async def list_action_dispatch_attempts(
        self, tenant_id: str, action_dispatch_id: UUID
    ) -> list[ActionDispatchAttempt]:
        action = self.action_dispatches.get(action_dispatch_id)
        if not action or action.tenant_id != tenant_id:
            return []
        return self.action_dispatch_attempts.get(action_dispatch_id, [])

    async def record_memory_audit(self, event: MemoryAuditEvent) -> MemoryAuditEvent:
        self.memory_audits.append(event)
        return event

    async def list_memory_audit(
        self, tenant_id: str, traveler_id: str | None = None
    ) -> list[MemoryAuditEvent]:
        return [
            event
            for event in self.memory_audits
            if event.tenant_id == tenant_id
            and (traveler_id is None or event.traveler_id == traveler_id)
        ]

    async def save_change_record(self, record: ChangeRecord) -> ChangeRecord:
        self.change_records[record.change_id] = record
        return record

    async def list_change_records(self, tenant_id: str) -> list[ChangeRecord]:
        return [record for record in self.change_records.values() if record.tenant_id == tenant_id]

    async def save_provider_onboarding(
        self, record: ProviderOnboardingRecord
    ) -> ProviderOnboardingRecord:
        self.provider_onboarding_records[record.provider_onboarding_id] = record
        return record

    async def list_provider_onboarding(self, tenant_id: str) -> list[ProviderOnboardingRecord]:
        return [
            record
            for record in self.provider_onboarding_records.values()
            if record.tenant_id == tenant_id
        ]

    async def get_runtime_control_override(
        self, tenant_id: str, control_name: RuntimeControlName
    ) -> RuntimeControlChange | None:
        record = self.runtime_control_overrides.get((tenant_id, control_name))
        if record and record.expires_at and record.expires_at <= datetime.now(UTC):
            return None
        return record

    async def save_runtime_control_change(
        self, record: RuntimeControlChange
    ) -> RuntimeControlChange:
        self.runtime_control_overrides[(record.tenant_id, record.control_name)] = record
        self.runtime_control_changes.append(record)
        return record

    async def list_runtime_control_changes(self, tenant_id: str) -> list[RuntimeControlChange]:
        return [record for record in self.runtime_control_changes if record.tenant_id == tenant_id]

    async def get_platform_control_override(
        self, control_name: RuntimeControlName
    ) -> PlatformRuntimeControlChange | None:
        record = self.platform_control_overrides.get(control_name)
        if record and record.expires_at and record.expires_at <= datetime.now(UTC):
            return None
        return record

    async def save_platform_control_change(
        self, record: PlatformRuntimeControlChange
    ) -> PlatformRuntimeControlChange:
        self.platform_control_overrides[record.control_name] = record
        self.platform_control_changes.append(record)
        return record

    async def list_platform_control_changes(self) -> list[PlatformRuntimeControlChange]:
        return list(self.platform_control_changes)

    async def save_original_upload(self, record: OriginalUploadRecord) -> OriginalUploadRecord:
        self.original_uploads[record.original_upload_id] = record
        return record

    async def get_original_upload(
        self, tenant_id: str, original_upload_id: UUID
    ) -> OriginalUploadRecord | None:
        record = self.original_uploads.get(original_upload_id)
        return record if record and record.tenant_id == tenant_id else None

    async def save_legal_hold(self, hold: LegalHoldRecord) -> LegalHoldRecord:
        self.legal_holds[hold.legal_hold_id] = hold
        return hold

    async def get_legal_hold(self, tenant_id: str, legal_hold_id: UUID) -> LegalHoldRecord | None:
        hold = self.legal_holds.get(legal_hold_id)
        return hold if hold and hold.tenant_id == tenant_id else None

    async def list_legal_holds(self, tenant_id: str) -> list[LegalHoldRecord]:
        return sorted(
            (hold for hold in self.legal_holds.values() if hold.tenant_id == tenant_id),
            key=lambda hold: (hold.created_at, str(hold.legal_hold_id)),
        )

    async def active_legal_holds_for_traveler(
        self, tenant_id: str, traveler_id: str, *, now: datetime | None = None
    ) -> list[LegalHoldRecord]:
        timestamp = now or datetime.now(UTC)
        trip_ids = {
            trip.trip_id
            for trip in self.trips.values()
            if trip.tenant_id == tenant_id and trip.traveler_id == traveler_id
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
        self.deletion_requests[request.deletion_request_id] = request
        return request

    async def get_deletion_request(
        self, tenant_id: str, deletion_request_id: UUID
    ) -> DeletionRequest | None:
        request = self.deletion_requests.get(deletion_request_id)
        return request if request and request.tenant_id == tenant_id else None

    async def list_deletion_requests(
        self, tenant_id: str, traveler_id: str | None = None
    ) -> list[DeletionRequest]:
        return sorted(
            (
                request
                for request in self.deletion_requests.values()
                if request.tenant_id == tenant_id
                and (traveler_id is None or request.traveler_id == traveler_id)
            ),
            key=lambda request: (request.submitted_at, str(request.deletion_request_id)),
        )

    async def list_pending_deletion_requests(self) -> list[DeletionRequest]:
        return sorted(
            (
                request
                for request in self.deletion_requests.values()
                if request.status
                in {DeletionRequestStatus.PENDING, DeletionRequestStatus.BLOCKED_BY_LEGAL_HOLD}
            ),
            key=lambda request: (request.submitted_at, str(request.deletion_request_id)),
        )

    async def erase_traveler_data(
        self,
        tenant_id: str,
        traveler_id: str,
        *,
        scope: DeletionRequestScope = DeletionRequestScope.TRAVELER_DATA,
    ) -> dict[str, int]:
        """Erase scoped traveler data while retaining minimal security-audit records.

        The caller must check legal holds before invoking this method.  The
        operation is naturally idempotent so worker retries remain safe.
        """
        memory_counts = await self.delete_traveler_memory(tenant_id, traveler_id)
        counts = {
            "profiles": memory_counts["profiles"],
            "memory_proposals": memory_counts["memory_proposals"],
        }
        if scope == DeletionRequestScope.PREFERENCE_MEMORY:
            return counts
        trip_ids = {
            trip_id
            for trip_id, trip in self.trips.items()
            if trip.tenant_id == tenant_id and trip.traveler_id == traveler_id
        }
        incident_ids = {
            incident_id
            for incident_id, incident in self.incidents.items()
            if incident.tenant_id == tenant_id and incident.trip_id in trip_ids
        }
        counts.update(
            {
                "trips": len(trip_ids),
                "evidence": sum(1 for trip_id in self.evidence if trip_id in trip_ids),
                "assessments": sum(1 for trip_id in self.assessments if trip_id in trip_ids),
                "incidents": len(incident_ids),
            }
        )
        for trip_id in trip_ids:
            self.trips.pop(trip_id, None)
            self.trip_segments.pop(trip_id, None)
            self.evidence.pop(trip_id, None)
            self.assessments.pop(trip_id, None)
        for incident_id in incident_ids:
            self.incidents.pop(incident_id, None)
            self.approvals.pop(incident_id, None)
        self.candidate_sets = {
            candidate_set_id: item
            for candidate_set_id, item in self.candidate_sets.items()
            if item[1].incident_id not in incident_ids
        }
        self.candidate_outcomes = {
            outcome_id: item
            for outcome_id, item in self.candidate_outcomes.items()
            if item[1].incident_id not in incident_ids
        }
        self.manager_feedback = {
            feedback_id: feedback
            for feedback_id, feedback in self.manager_feedback.items()
            if feedback.incident_id not in incident_ids
        }
        self.notifications = {
            notification_id: notification
            for notification_id, notification in self.notifications.items()
            if not (
                notification.tenant_id == tenant_id and notification.traveler_id == traveler_id
            )
        }
        self.notification_attempts = {
            notification_id: attempts
            for notification_id, attempts in self.notification_attempts.items()
            if notification_id in self.notifications
        }
        self.action_dispatches = {
            action_id: action
            for action_id, action in self.action_dispatches.items()
            if action.incident_id not in incident_ids
        }
        self.action_dispatch_attempts = {
            action_id: attempts
            for action_id, attempts in self.action_dispatch_attempts.items()
            if action_id in self.action_dispatches
        }
        return counts

    async def purge_expired(
        self,
        before: datetime,
        *,
        original_upload_before: datetime | None = None,
        audit_before: datetime | None = None,
    ) -> dict[str, int]:
        """Delete expired tenant records and return only aggregate counts.

        The operation is intentionally irreversible and therefore never exposes
        deleted payloads in its result or audit trail.
        """
        original_upload_cutoff = original_upload_before or before
        audit_cutoff = audit_before or before
        active_holds = [hold for hold in self.legal_holds.values() if hold.is_active()]
        held_trip_ids = {
            trip_id
            for trip_id, trip in self.trips.items()
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
        expired_trip_ids = {
            trip_id for trip_id, trip in self.trips.items() if trip.created_at < before
        } - held_trip_ids
        expired_incident_ids = {
            incident_id
            for incident_id, incident in self.incidents.items()
            if incident.trip_id in expired_trip_ids
        }
        counts = {
            "trips": len(expired_trip_ids),
            "evidence": sum(1 for trip_id in self.evidence if trip_id in expired_trip_ids),
            "assessments": sum(1 for trip_id in self.assessments if trip_id in expired_trip_ids),
            "incidents": len(expired_incident_ids),
            "profiles": sum(
                1
                for profile in self.profiles.values()
                if profile.updated_at < before
                and (profile.tenant_id, profile.traveler_id) not in held_travelers
                and profile.tenant_id not in held_tenants
            ),
            "memory_proposals": sum(
                1
                for proposal in self.memory_proposals.values()
                if proposal.created_at < before
                and (proposal.tenant_id, proposal.traveler_id) not in held_travelers
                and proposal.tenant_id not in held_tenants
            ),
            "notifications": sum(
                1
                for notification in self.notifications.values()
                if notification.created_at < before
            ),
            "original_uploads": sum(
                1
                for upload in self.original_uploads.values()
                if upload.created_at < original_upload_cutoff
                and upload.tenant_id not in held_tenants
            ),
            "audit_events": sum(1 for event in self.events if event.emitted_at < audit_cutoff),
            "memory_audits": sum(
                1 for event in self.memory_audits if event.occurred_at < audit_cutoff
            ),
        }
        for trip_id in expired_trip_ids:
            self.trips.pop(trip_id, None)
            self.evidence.pop(trip_id, None)
            self.assessments.pop(trip_id, None)
        for incident_id in expired_incident_ids:
            self.incidents.pop(incident_id, None)
            self.approvals.pop(incident_id, None)
        self.candidate_sets = {
            candidate_id: item
            for candidate_id, item in self.candidate_sets.items()
            if item[1].incident_id not in expired_incident_ids
        }
        self.candidate_outcomes = {
            outcome_id: item
            for outcome_id, item in self.candidate_outcomes.items()
            if item[1].incident_id not in expired_incident_ids
        }
        self.manager_feedback = {
            feedback_id: feedback
            for feedback_id, feedback in self.manager_feedback.items()
            if feedback.incident_id not in expired_incident_ids
        }
        self.profiles = {
            key: profile
            for key, profile in self.profiles.items()
            if profile.updated_at >= before
            or key in held_travelers
            or profile.tenant_id in held_tenants
        }
        self.memory_proposals = {
            proposal_id: proposal
            for proposal_id, proposal in self.memory_proposals.items()
            if proposal.created_at >= before
            or (proposal.tenant_id, proposal.traveler_id) in held_travelers
            or proposal.tenant_id in held_tenants
        }
        expired_notification_ids = {
            notification_id
            for notification_id, notification in self.notifications.items()
            if notification.created_at < before
        }
        self.notifications = {
            notification_id: notification
            for notification_id, notification in self.notifications.items()
            if notification_id not in expired_notification_ids
        }
        self.notification_attempts = {
            notification_id: attempts
            for notification_id, attempts in self.notification_attempts.items()
            if notification_id not in expired_notification_ids
        }
        self.original_uploads = {
            upload_id: upload
            for upload_id, upload in self.original_uploads.items()
            if upload.created_at >= original_upload_cutoff or upload.tenant_id in held_tenants
        }
        self.action_dispatches = {
            action_id: action
            for action_id, action in self.action_dispatches.items()
            if action.incident_id not in expired_incident_ids
        }
        self.action_dispatch_attempts = {
            action_id: attempts
            for action_id, attempts in self.action_dispatch_attempts.items()
            if action_id in self.action_dispatches
        }
        self.events = [event for event in self.events if event.emitted_at >= audit_cutoff]
        self.memory_audits = [
            event for event in self.memory_audits if event.occurred_at >= audit_cutoff
        ]
        self.idempotency_records = {
            key: record
            for key, record in self.idempotency_records.items()
            if record.expires_at >= before
        }
        return counts
