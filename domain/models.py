"""Validated domain contracts shared by the API, graph, and provider adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Severity(StrEnum):
    LOW = "low"
    WATCH = "watch"
    HIGH = "high"
    CRITICAL = "critical"


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    CACHED = "cached"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class TripCriticality(StrEnum):
    STANDARD = "standard"
    IMPORTANT = "important"
    BUSINESS_CRITICAL = "business_critical"


class TripStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


class AssessmentDisposition(StrEnum):
    MONITOR = "monitor"
    MANAGER_QUEUE = "manager_queue"
    INVESTIGATE = "investigate"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class IncidentStatus(StrEnum):
    OPEN = "open"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESOLVED = "resolved"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class MemoryProposalStatus(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ManagerFeedbackType(StrEnum):
    MANAGER_OVERRIDE = "manager_override"
    RECOVERY_OUTCOME = "recovery_outcome"


class NotificationStatus(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    ACKNOWLEDGED = "acknowledged"
    CANCELLED = "cancelled"


class ActionDispatchStatus(StrEnum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class ChangeRecordType(StrEnum):
    MODEL = "model"
    PROMPT = "prompt"
    POLICY = "policy"


class ProviderOnboardingStatus(StrEnum):
    DRAFT = "draft"
    SECURITY_REVIEW = "security_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISABLED = "disabled"


class DeletionRequestStatus(StrEnum):
    PENDING = "pending"
    BLOCKED_BY_LEGAL_HOLD = "blocked_by_legal_hold"
    COMPLETED = "completed"
    FAILED = "failed"


class DeletionRequestScope(StrEnum):
    PREFERENCE_MEMORY = "preference_memory"
    TRAVELER_DATA = "traveler_data"


class LegalHoldScope(StrEnum):
    TENANT = "tenant"
    TRAVELER = "traveler"
    TRIP = "trip"


class IdempotencyState(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class UserRole(StrEnum):
    TRAVELER = "traveler"
    TRAVEL_MANAGER = "travel_manager"
    DUTY_OF_CARE = "duty_of_care"
    TENANT_ADMIN = "tenant_admin"
    PLATFORM_ADMIN = "platform_admin"


class RuntimeControlName(StrEnum):
    LLM_ENABLED = "LLM_ENABLED"
    REACT_TOOL_CALLS_ENABLED = "REACT_TOOL_CALLS_ENABLED"
    NOTIFICATIONS_ENABLED = "NOTIFICATIONS_ENABLED"
    APPROVAL_ACTIONS_ENABLED = "APPROVAL_ACTIONS_ENABLED"
    MEMORY_READS_ENABLED = "MEMORY_READS_ENABLED"
    MEMORY_WRITES_ENABLED = "MEMORY_WRITES_ENABLED"
    PROVIDER_AMADEUS_ENABLED = "PROVIDER_AMADEUS_ENABLED"
    PROVIDER_FAA_ENABLED = "PROVIDER_FAA_ENABLED"
    PROVIDER_NWS_ENABLED = "PROVIDER_NWS_ENABLED"
    PROVIDER_AVIATION_WEATHER_ENABLED = "PROVIDER_AVIATION_WEATHER_ENABLED"
    PROVIDER_GOOGLE_ROUTES_ENABLED = "PROVIDER_GOOGLE_ROUTES_ENABLED"
    PROVIDER_DESTINATION_ADVISORY_ENABLED = "PROVIDER_DESTINATION_ADVISORY_ENABLED"
    TENANT_AUTOMATION_ENABLED = "TENANT_AUTOMATION_ENABLED"


class OriginalUploadStatus(StrEnum):
    QUARANTINED = "quarantined"
    VALIDATED = "validated"
    REJECTED = "rejected"


class FlightSegmentCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    segment_id: str = Field(min_length=1, max_length=64)
    carrier_code: str = Field(pattern=r"^[A-Z0-9]{2,3}$")
    flight_number: str = Field(pattern=r"^[0-9]{1,4}$")
    departure_airport: str = Field(pattern=r"^[A-Z]{3}$")
    arrival_airport: str = Field(pattern=r"^[A-Z]{3}$")
    scheduled_departure_at: datetime
    scheduled_arrival_at: datetime

    @model_validator(mode="after")
    def validate_schedule(self) -> FlightSegmentCreate:
        if self.scheduled_departure_at.tzinfo is None or self.scheduled_arrival_at.tzinfo is None:
            raise ValueError("scheduled timestamps must include a UTC offset")
        if self.scheduled_arrival_at <= self.scheduled_departure_at:
            raise ValueError("scheduled arrival must be after departure")
        return self


class TripCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    traveler_id: str = Field(min_length=1, max_length=128)
    assigned_manager_id: str | None = Field(default=None, max_length=128)
    trip_criticality: TripCriticality = TripCriticality.STANDARD
    ground_origin: str = Field(min_length=1, max_length=256)
    destination_country: str = Field(pattern=r"^[A-Z]{2}$")
    segments: list[FlightSegmentCreate] = Field(min_length=1, max_length=3)

    @field_validator("segments")
    @classmethod
    def validate_segment_order(
        cls, segments: list[FlightSegmentCreate]
    ) -> list[FlightSegmentCreate]:
        for previous, current in zip(segments, segments[1:]):
            if previous.arrival_airport != current.departure_airport:
                raise ValueError(
                    "each segment must depart from the prior segment's arrival airport"
                )
            if current.scheduled_departure_at < previous.scheduled_arrival_at:
                raise ValueError("segments must be in chronological order")
        return segments


class Trip(TripCreate):
    trip_id: UUID = Field(default_factory=uuid4)
    status: TripStatus = TripStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TripAssignmentRequest(BaseModel):
    manager_id: str = Field(min_length=1, max_length=128)


class TravelerIncidentView(BaseModel):
    """A traveler-safe incident representation with no policy or manager rationale."""

    incident_id: UUID
    trip_id: UUID
    severity: Severity
    status: IncidentStatus
    created_at: datetime
    updated_at: datetime
    approved_guidance: str | None = None


class OriginalUploadRecord(BaseModel):
    """Metadata for a restricted original import; raw bytes remain in object storage only."""

    original_upload_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    source: str = Field(pattern=r"^(csv|booking_webhook)$")
    object_key: str = Field(min_length=1, max_length=512)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_size_bytes: int = Field(ge=1, le=10_000_000)
    content_type: str = Field(min_length=1, max_length=128)
    status: OriginalUploadStatus = OriginalUploadStatus.QUARANTINED
    validation_errors: list[str] = Field(default_factory=list, max_length=20)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    validated_at: datetime | None = None


class EvidenceEnvelope(BaseModel):
    """Normalized read-only source result. Raw payload is kept out of graph state."""

    evidence_id: UUID = Field(default_factory=uuid4)
    source_name: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    source_url_or_record_id: str = Field(min_length=1)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    observed_at: datetime | None = None
    expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=15))
    freshness_status: FreshnessStatus = FreshnessStatus.FRESH
    normalized_payload: dict[str, object] = Field(default_factory=dict)
    raw_payload_reference: str | None = None
    provider_latency_ms: int | None = Field(default=None, ge=0)
    provider_request_id: str | None = None
    error_code: str | None = None


class RiskFactors(BaseModel):
    flight_disruption: float = Field(ge=0, le=100)
    connection_fragility: float = Field(ge=0, le=100)
    airport_weather: float = Field(ge=0, le=100)
    ground_route_disruption: float = Field(ge=0, le=100)
    destination_advisory: float = Field(ge=0, le=100)
    traveler_trip_criticality: float = Field(ge=0, le=100)


class RiskAssessment(BaseModel):
    assessment_id: UUID = Field(default_factory=uuid4)
    trip_id: UUID
    policy_version: str
    risk_score: float = Field(ge=0, le=100)
    severity: Severity
    factor_contributions: dict[str, float]
    assessment_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    evidence_ids: list[UUID]
    unknown_factors: list[str] = Field(default_factory=list)
    uncertainty: str = "low"


class SourceHealth(BaseModel):
    core_sources_unavailable: list[str] = Field(default_factory=list)
    limited_visibility: bool = False


class AssessmentResult(BaseModel):
    assessment: RiskAssessment
    evidence: list[EvidenceEnvelope]
    source_health: SourceHealth
    disposition: AssessmentDisposition
    incident_id: UUID | None = None


class ToolAuditEvent(BaseModel):
    tool_name: str
    arguments: dict[str, object] = Field(default_factory=dict)
    evidence_ids: list[UUID] = Field(default_factory=list)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    outcome: str = "completed"


class ModelInvocationAudit(BaseModel):
    """Redacted model invocation metadata retained with the incident."""

    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=128)
    invocation_type: str = Field(min_length=1, max_length=128)
    outcome: str = Field(min_length=1, max_length=128)
    input_summary: dict[str, object] = Field(default_factory=dict)
    token_usage: dict[str, int] | None = None
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Recommendation(BaseModel):
    recommendation_id: UUID = Field(default_factory=uuid4)
    incident_id: UUID
    severity_explanation: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[UUID] = Field(min_length=1)
    uncertainty: str = "high"
    recommended_action: str = Field(min_length=1, max_length=1000)
    ranked_alternative_ids: list[str] = Field(default_factory=list)
    traveler_message: str = Field(min_length=1, max_length=1000)
    manager_message: str = Field(min_length=1, max_length=2000)
    requires_human_approval: bool = True
    missing_information: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Incident(BaseModel):
    incident_id: UUID = Field(default_factory=uuid4)
    tenant_id: str
    trip_id: UUID
    assessment_id: UUID
    severity: Severity
    status: IncidentStatus = IncidentStatus.OPEN
    correlation_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tool_audit: list[ToolAuditEvent] = Field(default_factory=list)
    model_audit: list[ModelInvocationAudit] = Field(default_factory=list)
    recommendation: Recommendation | None = None
    graph_thread_id: str | None = None
    approval_payload: dict[str, object] | None = None


class ApprovalRequest(BaseModel):
    decision: ApprovalDecision
    actor_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)
    final_action_payload: dict[str, object] = Field(default_factory=dict)


class ApprovalRecord(ApprovalRequest):
    approval_id: UUID = Field(default_factory=uuid4)
    incident_id: UUID
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    action_dispatch_status: str = Field(
        default="not_requested", pattern=r"^(not_requested|suppressed|pending|dispatched|failed)$"
    )


class EventRecord(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    event_type: str
    tenant_id: str
    trip_id: UUID | None = None
    correlation_id: UUID
    idempotency_key: str
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor_id: str | None = None
    details: dict[str, object] = Field(default_factory=dict)


class IdempotencyRecord(BaseModel):
    """A tenant-scoped durable result for an unsafe request or job delivery."""

    tenant_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    scope: str = Field(min_length=1, max_length=256)
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: IdempotencyState = IdempotencyState.IN_PROGRESS
    response_status_code: int | None = Field(default=None, ge=100, le=599)
    response_payload: dict[str, object] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    completed_at: datetime | None = None


class TravelerPreferenceProfile(BaseModel):
    tenant_id: str
    traveler_id: str
    preferred_airports: list[str] = Field(default_factory=list)
    preferred_carriers: list[str] = Field(default_factory=list)
    cabin_or_seat_preference: str | None = None
    minimum_connection_minutes: int | None = Field(default=None, ge=0, le=480)
    avoid_overnight_connections: bool | None = None
    approved_ground_transport_preferences: list[str] = Field(default_factory=list)
    notification_channel: str | None = None
    language: str = "en"
    approved_accessibility_accommodations: list[str] = Field(default_factory=list)
    consent_version: str = Field(min_length=1, max_length=64)
    version: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TravelerPreferenceUpdate(BaseModel):
    """An explicit settings change from an authenticated traveler or tenant admin."""

    preferred_airports: list[str] | None = None
    preferred_carriers: list[str] | None = None
    cabin_or_seat_preference: str | None = None
    minimum_connection_minutes: int | None = Field(default=None, ge=0, le=480)
    avoid_overnight_connections: bool | None = None
    approved_ground_transport_preferences: list[str] | None = None
    notification_channel: str | None = None
    language: str | None = None
    approved_accessibility_accommodations: list[str] | None = None
    consent_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def includes_a_preference_change(self) -> TravelerPreferenceUpdate:
        if not any(
            value is not None
            for field, value in self.model_dump().items()
            if field != "consent_version"
        ):
            raise ValueError("at least one preference field must be supplied")
        return self


class MemoryUpdateProposal(BaseModel):
    proposal_id: UUID = Field(default_factory=uuid4)
    tenant_id: str
    traveler_id: str
    memory_type: str = "traveler_profile"
    record_id: str
    before_value: dict[str, object] = Field(default_factory=dict)
    patch: dict[str, object] = Field(min_length=1)
    source_message_id: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0, le=1)
    consent_required: bool = True
    status: MemoryProposalStatus = MemoryProposalStatus.PENDING_CONFIRMATION
    actor_id: str = Field(min_length=1, max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None


class NotificationRecord(BaseModel):
    """A rendered, approval-safe notification and its delivery state.

    ``recipient_reference`` is a token/opaque provider reference, never an email
    address or phone number.  Delivery integrations resolve it inside their own
    approved boundary.
    """

    notification_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    incident_id: UUID | None = None
    traveler_id: str = Field(min_length=1, max_length=128)
    channel: str = Field(pattern=r"^(email|sms|push|in_app)$")
    recipient_reference: str = Field(min_length=1, max_length=256)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    status: NotificationStatus = NotificationStatus.QUEUED
    attempt_count: int = Field(default=0, ge=0, le=10)
    next_attempt_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None
    acknowledgement_actor_id: str | None = Field(default=None, max_length=128)
    last_error_code: str | None = Field(default=None, max_length=128)
    dispatch_claim_id: UUID | None = None
    dispatch_claimed_by: str | None = Field(default=None, max_length=128)
    dispatch_claimed_at: datetime | None = None
    dispatch_claim_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NotificationCreate(BaseModel):
    incident_id: UUID | None = None
    traveler_id: str = Field(min_length=1, max_length=128)
    channel: str = Field(pattern=r"^(email|sms|push|in_app)$")
    recipient_reference: str = Field(min_length=1, max_length=256)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)


class NotificationAttempt(BaseModel):
    attempt_id: UUID = Field(default_factory=uuid4)
    notification_id: UUID
    attempt_number: int = Field(ge=1, le=10)
    attempted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    outcome: str = Field(pattern=r"^(delivered|retry_scheduled|failed)$")
    provider_message_reference: str | None = Field(default=None, max_length=256)
    error_code: str | None = Field(default=None, max_length=128)


class ActionDispatchRecord(BaseModel):
    """A human-approved, idempotent external-action request waiting in the outbox."""

    action_dispatch_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    incident_id: UUID
    approval_id: UUID
    action_payload: dict[str, object] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    status: ActionDispatchStatus = ActionDispatchStatus.QUEUED
    attempt_count: int = Field(default=0, ge=0, le=10)
    next_attempt_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_error_code: str | None = Field(default=None, max_length=128)
    dispatch_claim_id: UUID | None = None
    dispatch_claimed_by: str | None = Field(default=None, max_length=128)
    dispatch_claimed_at: datetime | None = None
    dispatch_claim_expires_at: datetime | None = None
    dispatched_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ActionDispatchAttempt(BaseModel):
    attempt_id: UUID = Field(default_factory=uuid4)
    action_dispatch_id: UUID
    attempt_number: int = Field(ge=1, le=10)
    attempted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    outcome: str = Field(pattern=r"^(dispatched|retry_scheduled|failed|suppressed)$")
    external_reference: str | None = Field(default=None, max_length=256)
    error_code: str | None = Field(default=None, max_length=128)


class MemoryAuditEvent(BaseModel):
    audit_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    traveler_id: str = Field(min_length=1, max_length=128)
    action: str = Field(pattern=r"^(proposed|confirmed|updated|rejected|deleted|exported)$")
    proposal_id: UUID | None = None
    actor_id: str | None = Field(default=None, max_length=128)
    details: dict[str, object] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ManagerFeedback(BaseModel):
    """Structured operational feedback, deliberately separate from traveler memory."""

    feedback_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    incident_id: UUID
    candidate_set_id: UUID
    candidate_id: str = Field(min_length=1)
    feedback_type: ManagerFeedbackType
    outcome_id: UUID | None = None
    actor_id: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=1000)
    details: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_feedback(self) -> ManagerFeedback:
        if self.feedback_type == ManagerFeedbackType.MANAGER_OVERRIDE and not self.reason:
            raise ValueError("manager override feedback requires a reason")
        if self.feedback_type == ManagerFeedbackType.RECOVERY_OUTCOME and not self.outcome_id:
            raise ValueError("recovery outcome feedback requires an outcome reference")
        return self


class TenantPlaybookCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    version: str = Field(min_length=1, max_length=64)
    guidance: str = Field(min_length=1, max_length=5000)


class TenantPlaybook(TenantPlaybookCreate):
    """An immutable tenant-admin-approved procedural guidance version."""

    playbook_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChangeRecord(BaseModel):
    change_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    change_type: ChangeRecordType
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=2000)
    risk_assessment: str = Field(min_length=1, max_length=2000)
    rollback_plan: str = Field(min_length=1, max_length=2000)
    evidence_reference: str | None = Field(default=None, max_length=512)
    requested_by: str = Field(min_length=1, max_length=128)
    approved_by: str | None = Field(default=None, max_length=128)
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RuntimeControlUpdate(BaseModel):
    enabled: bool
    reason: str = Field(min_length=1, max_length=2000)
    review_at: datetime | None = None
    expires_at: datetime | None = None


class RuntimeControlChange(RuntimeControlUpdate):
    control_change_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    scope: str = Field(default="tenant", pattern=r"^tenant$")
    control_name: RuntimeControlName
    previous_enabled: bool
    actor_id: str = Field(min_length=1, max_length=128)
    changed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PlatformRuntimeControlChange(RuntimeControlUpdate):
    """A platform default with no tenant scope and no tenant-data access path."""

    control_change_id: UUID = Field(default_factory=uuid4)
    scope: str = Field(default="platform", pattern=r"^platform$")
    control_name: RuntimeControlName
    previous_enabled: bool
    actor_id: str = Field(min_length=1, max_length=128)
    changed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProviderOnboardingRecord(BaseModel):
    provider_onboarding_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    provider_name: str = Field(min_length=1, max_length=128)
    purpose: str = Field(min_length=1, max_length=1000)
    data_classification: str = Field(min_length=1, max_length=256)
    contract_reference: str | None = Field(default=None, max_length=512)
    quota_reference: str | None = Field(default=None, max_length=512)
    owner_id: str = Field(min_length=1, max_length=128)
    status: ProviderOnboardingStatus = ProviderOnboardingStatus.DRAFT
    approved_by: str | None = Field(default=None, max_length=128)
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LegalHoldRecord(BaseModel):
    """An auditable, narrowly scoped deletion hold.

    A hold can cover an entire tenant, one traveler, or one trip.  It is never
    inferred from free-form request text, which keeps the retention worker from
    silently retaining unrelated tenant data.
    """

    legal_hold_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    scope: LegalHoldScope
    reason: str = Field(min_length=1, max_length=2000)
    created_by: str = Field(min_length=1, max_length=128)
    traveler_id: str | None = Field(default=None, max_length=128)
    trip_id: UUID | None = None
    expires_at: datetime | None = None
    released_at: datetime | None = None
    released_by: str | None = Field(default=None, max_length=128)
    release_reason: str | None = Field(default=None, max_length=2000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_scope(self) -> LegalHoldRecord:
        if self.scope == LegalHoldScope.TENANT and (self.traveler_id or self.trip_id):
            raise ValueError("tenant legal holds cannot include traveler_id or trip_id")
        if self.scope == LegalHoldScope.TRAVELER and not self.traveler_id:
            raise ValueError("traveler legal holds require traveler_id")
        if self.scope == LegalHoldScope.TRAVELER and self.trip_id:
            raise ValueError("traveler legal holds cannot include trip_id")
        if self.scope == LegalHoldScope.TRIP and not self.trip_id:
            raise ValueError("trip legal holds require trip_id")
        if self.scope == LegalHoldScope.TRIP and self.traveler_id:
            raise ValueError("trip legal holds cannot include traveler_id")
        if self.released_at and not self.released_by:
            raise ValueError("released legal holds require released_by")
        return self

    def is_active(self, now: datetime | None = None) -> bool:
        timestamp = now or datetime.now(UTC)
        return (
            self.released_at is None
            and (self.expires_at is None or self.expires_at > timestamp)
        )


class LegalHoldRelease(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class DeletionRequest(BaseModel):
    """A DSAR-style erasure request whose execution is explicit and auditable."""

    deletion_request_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(min_length=1, max_length=128)
    traveler_id: str = Field(min_length=1, max_length=128)
    scope: DeletionRequestScope = DeletionRequestScope.TRAVELER_DATA
    requested_by: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    status: DeletionRequestStatus = DeletionRequestStatus.PENDING
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    blocked_by_hold_ids: list[UUID] = Field(default_factory=list)
    failure_code: str | None = Field(default=None, max_length=128)
