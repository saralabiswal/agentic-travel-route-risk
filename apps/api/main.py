"""Safe MVP HTTP surface for itinerary ingestion and deterministic assessments."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from starlette.requests import Request

from agent.checkpointer import postgres_checkpointer
from agent.graph import build_graph
from agent.state import thread_id_for
from apps.api.actions import UnconfiguredActionSender, dispatch_approved_action
from apps.api.auth import validate_bearer_token
from apps.api.config import RuntimeControls
from apps.api.idempotency import request_fingerprint
from apps.api.ingestion import (
    parse_booking_webhook,
    parse_csv_rows,
    parse_webhook_timestamp,
    verify_webhook_signature,
)
from apps.api.notifications import InAppNotificationSender, deliver_notification
from apps.api.postgres_repository import PostgresRouteShieldRepository
from apps.api.repository import InMemoryRouteShieldRepository
from apps.api.security import FixedWindowRateLimiter, RedisFixedWindowRateLimiter, redact
from apps.api.uploads import GcsOriginalUploadStore, InMemoryOriginalUploadStore
from domain.models import (
    ActionDispatchRecord,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    AssessmentDisposition,
    AssessmentResult,
    ChangeRecord,
    DeletionRequest,
    DeletionRequestStatus,
    EventRecord,
    EvidenceEnvelope,
    FreshnessStatus,
    Incident,
    IncidentStatus,
    LegalHoldRecord,
    LegalHoldRelease,
    ManagerFeedback,
    ManagerFeedbackType,
    MemoryAuditEvent,
    MemoryProposalStatus,
    MemoryUpdateProposal,
    ModelInvocationAudit,
    NotificationAttempt,
    NotificationCreate,
    NotificationRecord,
    NotificationStatus,
    OriginalUploadRecord,
    OriginalUploadStatus,
    PlatformRuntimeControlChange,
    ProviderOnboardingRecord,
    Recommendation,
    RiskFactors,
    RuntimeControlChange,
    RuntimeControlName,
    RuntimeControlUpdate,
    SourceHealth,
    TenantPlaybook,
    TenantPlaybookCreate,
    ToolAuditEvent,
    TravelerIncidentView,
    TravelerPreferenceProfile,
    TravelerPreferenceUpdate,
    Trip,
    TripAssignmentRequest,
    TripCreate,
    TripStatus,
    UserRole,
)
from domain.policies import CorporateTravelPolicy, evaluate_policy_eligible_candidate
from domain.recovery import (
    EvaluatedCandidate,
    RecoveryCandidateOutcome,
    RecoveryCandidateOutcomeState,
    RecoveryCandidateOutcomeUpdate,
    RecoveryCandidateSet,
    RecoveryCandidateSetCreate,
    project_candidate_set_outcomes,
    rank_eligible,
    transition_allowed,
)
from domain.risk_engine import calculate_risk
from tools.alternatives import fixture_alternatives
from tools.evidence import DemoScenario, FixtureEvidenceCollector
from tools.live_providers import LiveEvidenceCollector
from workers.monitor_due_trips import assessment_due_idempotency_key, due_monitoring_windows
from workers.privacy import process_due_deletion_requests
from workers.retention import run_retention

repository = InMemoryRouteShieldRepository()
controls = RuntimeControls.from_environment()
route_risk_graph = build_graph()
rate_limiter = FixedWindowRateLimiter(
    limit=controls.api_rate_limit,
    window=timedelta(seconds=controls.api_rate_limit_window_seconds),
)
shared_rate_limiter: RedisFixedWindowRateLimiter | None = None
live_evidence_collector = LiveEvidenceCollector()
original_upload_store = (
    GcsOriginalUploadStore(os.environ["EVIDENCE_BUCKET"])
    if os.getenv("EVIDENCE_BUCKET")
    else InMemoryOriginalUploadStore()
)
recovery_policy = CorporateTravelPolicy()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global repository, route_risk_graph, shared_rate_limiter
    database_url = os.getenv("DATABASE_URL")
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        candidate_limiter = RedisFixedWindowRateLimiter(
            redis_url,
            limit=controls.api_rate_limit,
            window=timedelta(seconds=controls.api_rate_limit_window_seconds),
        )
        try:
            await candidate_limiter.ping()
        except Exception:
            if controls.rate_limit_redis_required:
                raise
        else:
            shared_rate_limiter = candidate_limiter
    if database_url:
        repository = PostgresRouteShieldRepository(database_url)
        await repository.setup()
        await audit_runtime_controls()
        async with postgres_checkpointer(database_url) as saver:
            route_risk_graph = build_graph(checkpointer=saver)
            try:
                yield
            finally:
                await repository.close()
                if shared_rate_limiter:
                    await shared_rate_limiter.close()
                    shared_rate_limiter = None
        return
    await audit_runtime_controls()
    try:
        yield
    finally:
        if shared_rate_limiter:
            await shared_rate_limiter.close()
            shared_rate_limiter = None


app = FastAPI(title="RouteShield API", version="0.2.0", lifespan=lifespan)
web_origins = [
    origin.strip() for origin in os.getenv("WEB_ORIGIN", "").split(",") if origin.strip()
]
if web_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=web_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-Tenant-Id",
            "X-Actor-Id",
            "X-Actor-Role",
        ],
    )


async def audit_runtime_controls() -> None:
    """Persist the effective kill-switch state at each worker/API start."""
    await repository.record_event(
        EventRecord(
            event_type="runtime_controls.loaded",
            tenant_id="system",
            trip_id=None,
            correlation_id=uuid4(),
            idempotency_key=f"runtime-controls:{datetime.now(UTC).isoformat()}",
            details={
                "llm_enabled": controls.llm_enabled,
                "react_tool_calls_enabled": controls.react_tool_calls_enabled,
                "notifications_enabled": controls.notifications_enabled,
                "approval_actions_enabled": controls.approval_actions_enabled,
                "memory_reads_enabled": controls.memory_reads_enabled,
                "memory_writes_enabled": controls.memory_writes_enabled,
                "tenant_automation_enabled": controls.tenant_automation_enabled,
                "provider_controls": {
                    "amadeus": os.getenv("PROVIDER_AMADEUS_ENABLED", "false").lower()
                    == "true",
                    "faa": os.getenv("PROVIDER_FAA_ENABLED", "false").lower() == "true",
                    "nws": os.getenv("PROVIDER_NWS_ENABLED", "false").lower() == "true",
                    "aviation_weather": os.getenv(
                        "PROVIDER_AVIATION_WEATHER_ENABLED", "false"
                    ).lower()
                    == "true",
                    "google_routes": os.getenv("PROVIDER_GOOGLE_ROUTES_ENABLED", "false").lower()
                    == "true",
                    "destination_advisory": os.getenv(
                        "PROVIDER_DESTINATION_ADVISORY_ENABLED", "false"
                    ).lower()
                    == "true",
                },
            },
        )
    )


async def emit_event(
    *,
    event_type: str,
    tenant_id: str,
    trip_id: UUID | None,
    correlation_id: UUID,
    actor_id: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    event = EventRecord(
        event_type=event_type,
        tenant_id=tenant_id,
        trip_id=trip_id,
        correlation_id=correlation_id,
        idempotency_key=f"{event_type}:{correlation_id}",
        actor_id=actor_id,
        details=redact(details or {}),
    )
    await repository.record_event(event)
    # Cloud Run converts one-line JSON logs into structured Cloud Logging entries.
    # Keep only correlation metadata here; sensitive details remain in the audited,
    # access-controlled data store and are separately redacted before persistence.
    print(
        json.dumps(
            {
                "event_type": event.event_type,
                "tenant_id": event.tenant_id,
                "trip_id": str(event.trip_id) if event.trip_id else None,
                "correlation_id": str(event.correlation_id),
                "emitted_at": event.emitted_at.isoformat(),
            },
            sort_keys=True,
        )
    )


async def claim_mutation(
    *, tenant_id: str, idempotency_key: str, scope: str, payload: object
):
    """Claim an unsafe request before it can resume a graph or enqueue work twice."""
    fingerprint = request_fingerprint(scope=scope, payload=payload)
    disposition, record = await repository.claim_idempotency(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=scope,
        request_hash=fingerprint,
        expires_at=datetime.now(UTC) + timedelta(seconds=controls.idempotency_ttl_seconds),
    )
    if disposition == "execute":
        return fingerprint, None
    if disposition == "replay":
        return fingerprint, record
    if disposition == "conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used for a different request",
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="A request with this Idempotency-Key is still in progress",
    )


async def complete_mutation(
    *,
    tenant_id: str,
    idempotency_key: str,
    request_hash: str,
    response_status_code: int,
    response_payload: dict[str, object],
) -> None:
    await repository.complete_idempotency(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=response_status_code,
        response_payload=response_payload,
    )


async def claim_optional_mutation(
    *, tenant_id: str, idempotency_key: str | None, scope: str, payload: object
):
    """Require durable idempotency in production while preserving local MVP ergonomics."""
    if not idempotency_key:
        if controls.require_idempotency:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Idempotency-Key is required",
            )
        return None, None
    return await claim_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=scope,
        payload=payload,
    )


async def complete_optional_mutation(
    *,
    tenant_id: str,
    idempotency_key: str | None,
    request_hash: str | None,
    response_status_code: int,
    response_payload: dict[str, object],
) -> None:
    if idempotency_key and request_hash:
        await complete_mutation(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response_status_code=response_status_code,
            response_payload=response_payload,
        )


@app.middleware("http")
async def enforce_rate_limit(request: Request, call_next):
    is_health_check = request.url.path == "/health"
    is_signed_webhook = request.url.path == "/v1/webhooks/booking"
    is_internal_job = request.url.path.startswith("/v1/internal/")
    authorization = request.headers.get("Authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return Response(
                status_code=status.HTTP_401_UNAUTHORIZED, content="invalid bearer token"
            )
        try:
            authenticated_actor = validate_bearer_token(token)
        except Exception:
            return Response(
                status_code=status.HTTP_401_UNAUTHORIZED, content="invalid bearer token"
            )
        supplied_tenant = request.headers.get("X-Tenant-Id")
        if supplied_tenant and supplied_tenant != authenticated_actor.tenant_id:
            return Response(status_code=status.HTTP_403_FORBIDDEN, content="tenant claim mismatch")
        request.state.actor = authenticated_actor
        trusted_headers = [
            (key, value)
            for key, value in request.scope["headers"]
            if key.lower() not in {b"x-tenant-id", b"x-actor-id", b"x-actor-role"}
        ]
        trusted_headers.extend(
            [
                (b"x-tenant-id", authenticated_actor.tenant_id.encode()),
                (b"x-actor-id", authenticated_actor.actor_id.encode()),
                (b"x-actor-role", authenticated_actor.role.value.encode()),
            ]
        )
        request.scope["headers"] = trusted_headers
    elif controls.require_oidc and not (is_health_check or is_signed_webhook or is_internal_job):
        return Response(
            status_code=status.HTTP_401_UNAUTHORIZED, content="bearer token is required"
        )

    actor_identity = getattr(request.state, "actor", None)
    tenant = (
        actor_identity.tenant_id
        if actor_identity
        else request.headers.get("X-Tenant-Id", "anonymous")
    )
    actor = actor_identity.actor_id if actor_identity else request.headers.get("X-Actor-Id")
    client = request.client.host if request.client else "unknown"
    rate_limit_key = f"{tenant}:{actor or client}:{request.url.path}"
    try:
        decision = (
            await shared_rate_limiter.check(rate_limit_key)
            if shared_rate_limiter
            else rate_limiter.check(rate_limit_key)
        )
    except Exception:
        if controls.rate_limit_redis_required:
            return Response(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content="rate limit service unavailable",
            )
        decision = rate_limiter.check(rate_limit_key)
    if not is_health_check and not decision.allowed:
        return Response(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content="rate limit exceeded",
            headers={
                "Retry-After": str(decision.retry_after_seconds),
                "X-RateLimit-Limit": str(decision.limit),
                "X-RateLimit-Remaining": "0",
            },
        )
    response = await call_next(request)
    if not is_health_check:
        response.headers["X-RateLimit-Limit"] = str(decision.limit)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
    return response


def require_tenant(x_tenant_id: str | None) -> str:
    if not x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Tenant-Id is required",
        )
    return x_tenant_id


def require_role(x_actor_role: UserRole | None, *allowed: UserRole) -> None:
    if x_actor_role not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")


def require_actor_id(x_actor_id: str | None) -> str | None:
    """Require an immutable OIDC subject when production authentication is enabled."""
    if controls.require_oidc and not x_actor_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authenticated actor identity is required",
        )
    return x_actor_id


def require_actor_matches_payload(
    *, x_actor_id: str | None, payload_actor_id: str
) -> str:
    """Do not let a client attribute a state change to a different signed-in user."""
    actor_id = require_actor_id(x_actor_id)
    if actor_id and actor_id != payload_actor_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="actor identity mismatch")
    return actor_id or payload_actor_id


def require_trip_access(
    *,
    trip: Trip,
    x_actor_role: UserRole | None,
    x_actor_id: str | None,
    allowed_roles: tuple[UserRole, ...],
) -> None:
    """Apply least-privilege trip access, including traveler ownership in OIDC mode."""
    require_role(x_actor_role, *allowed_roles)
    actor_id = require_actor_id(x_actor_id)
    if x_actor_role == UserRole.TRAVELER and actor_id and actor_id != trip.traveler_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="trip access denied")
    if (
        x_actor_role == UserRole.TRAVEL_MANAGER
        and actor_id
        and trip.assigned_manager_id != actor_id
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="trip access denied")


def default_control_enabled(control_name: RuntimeControlName) -> bool:
    defaults = {
        RuntimeControlName.LLM_ENABLED: controls.llm_enabled,
        RuntimeControlName.REACT_TOOL_CALLS_ENABLED: controls.react_tool_calls_enabled,
        RuntimeControlName.NOTIFICATIONS_ENABLED: controls.notifications_enabled,
        RuntimeControlName.APPROVAL_ACTIONS_ENABLED: controls.approval_actions_enabled,
        RuntimeControlName.MEMORY_READS_ENABLED: controls.memory_reads_enabled,
        RuntimeControlName.MEMORY_WRITES_ENABLED: controls.memory_writes_enabled,
        RuntimeControlName.TENANT_AUTOMATION_ENABLED: controls.tenant_automation_enabled,
    }
    if control_name in defaults:
        return defaults[control_name]
    return os.getenv(control_name.value, "false").strip().lower() == "true"


async def control_enabled(tenant_id: str, control_name: RuntimeControlName) -> bool:
    override = await repository.get_runtime_control_override(tenant_id, control_name)
    if override:
        return override.enabled
    platform_override = await repository.get_platform_control_override(control_name)
    return platform_override.enabled if platform_override else default_control_enabled(control_name)


async def require_memory_reads_enabled(tenant_id: str) -> None:
    if not await control_enabled(tenant_id, RuntimeControlName.MEMORY_READS_ENABLED):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="memory reads disabled"
        )


async def require_incident_trip_access(
    *,
    tenant_id: str,
    incident_id: UUID,
    x_actor_role: UserRole | None,
    x_actor_id: str | None,
    allowed_roles: tuple[UserRole, ...],
) -> tuple[Incident, Trip]:
    incident = await repository.get_incident(tenant_id, incident_id)
    if not incident:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")
    trip = await repository.get_trip(tenant_id, incident.trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trip not found")
    require_trip_access(
        trip=trip,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=allowed_roles,
    )
    return incident, trip


def get_scoring_input(
    evidence: list[EvidenceEnvelope], trip: Trip
) -> tuple[RiskFactors, list[str]]:
    """Read provider-normalized score inputs; unavailable inputs are explicit unknowns."""
    fields = {
        "flight_disruption": "flight_status",
        "connection_fragility": "connection_feasibility",
        "airport_weather": "airport_weather",
        "ground_route_disruption": "ground_route",
        "destination_advisory": "destination_advisory",
    }
    scores: dict[str, float] = {}
    unknown: list[str] = []
    for field, source_type in fields.items():
        items = [item for item in evidence if item.source_type == source_type]
        available = [
            item
            for item in items
            if item.freshness_status
            not in {FreshnessStatus.STALE, FreshnessStatus.UNAVAILABLE}
        ]
        if not available:
            scores[field] = 0
            unknown.append(field)
        else:
            values = [
                item.normalized_payload.get("risk_score", 0)
                for item in available
                if isinstance(item.normalized_payload.get("risk_score", 0), (int, float))
            ]
            scores[field] = max((float(value) for value in values), default=0)
    scores["traveler_trip_criticality"] = {
        "standard": 20,
        "important": 60,
        "business_critical": 100,
    }[trip.trip_criticality.value]
    return RiskFactors(**scores), unknown


def source_health_for(evidence: list[EvidenceEnvelope]) -> SourceHealth:
    core_source_types = {"flight_status", "airport_weather", "ground_route"}
    unavailable = [
        source_type
        for source_type in core_source_types
        if not (items := [item for item in evidence if item.source_type == source_type])
        or all(
            item.freshness_status in {FreshnessStatus.STALE, FreshnessStatus.UNAVAILABLE}
            for item in items
        )
    ]
    return SourceHealth(
        core_sources_unavailable=unavailable,
        limited_visibility=bool(unavailable),
    )


async def collect_assessment_evidence(
    *, trip: Trip, scenario: DemoScenario | None, correlation_id: str
) -> list[EvidenceEnvelope]:
    """Use captured fixtures only in explicit demo mode; deployed runs use live adapters."""
    if scenario or controls.demo_evidence_enabled:
        return FixtureEvidenceCollector(scenario or DemoScenario.NORMAL).collect_baseline(
            trip, correlation_id
        )

    async def provider_enabled(flag: str) -> bool:
        return await control_enabled(trip.tenant_id, RuntimeControlName(flag))

    return await live_evidence_collector.collect(trip, correlation_id, provider_enabled)


async def process_due_assessments(*, now: datetime | None = None) -> dict[str, int]:
    """Claim and assess due windows once across retries and duplicate Pub/Sub deliveries."""
    assessed = 0
    replayed = 0
    skipped = 0
    for trip in await repository.list_trips():
        if not await control_enabled(trip.tenant_id, RuntimeControlName.TENANT_AUTOMATION_ENABLED):
            skipped += 1
            continue
        for window in due_monitoring_windows(trip, now=now):
            idempotency_key = assessment_due_idempotency_key(
                trip_id=str(trip.trip_id), window=window
            )
            request_hash, replay = await claim_mutation(
                tenant_id=trip.tenant_id,
                idempotency_key=idempotency_key,
                scope="internal:assessment-due",
                payload={"trip_id": str(trip.trip_id), "window": window},
            )
            if replay:
                replayed += 1
                continue
            result = await assess_trip(
                trip.trip_id,
                x_tenant_id=trip.tenant_id,
                x_actor_role=UserRole.TENANT_ADMIN,
                x_actor_id="system-monitor",
                idempotency_key=f"{idempotency_key}:assessment",
            )
            await complete_mutation(
                tenant_id=trip.tenant_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_status_code=status.HTTP_200_OK,
                response_payload={
                    "trip_id": str(trip.trip_id),
                    "window": window,
                    "assessment_id": str(result.assessment.assessment_id),
                },
            )
            await emit_event(
                event_type="assessment.due.completed",
                tenant_id=trip.tenant_id,
                trip_id=trip.trip_id,
                correlation_id=uuid4(),
                actor_id="system-monitor",
                details={"window": window},
            )
            assessed += 1
    return {"assessed": assessed, "replayed": replayed, "skipped": skipped}


def disposition_for(assessment, health: SourceHealth) -> AssessmentDisposition:
    # A high/critical assessment without flight status cannot safely produce an
    # automated all-clear or a model-led recommendation.  The PRD requires an
    # explicit manager review even when every other source remains healthy.
    if (
        assessment.severity.value in {"high", "critical"}
        and "flight_status" in health.core_sources_unavailable
    ):
        return AssessmentDisposition.NEEDS_HUMAN_REVIEW
    if len(health.core_sources_unavailable) >= 2:
        return AssessmentDisposition.NEEDS_HUMAN_REVIEW
    if assessment.severity.value in {"high", "critical"}:
        return AssessmentDisposition.INVESTIGATE
    if assessment.severity.value == "watch":
        return AssessmentDisposition.MANAGER_QUEUE
    return AssessmentDisposition.MONITOR


def investigate(incident: Incident, evidence: list[EvidenceEnvelope]) -> Incident:
    """A bounded, server-owned fallback investigation for local/disabled-LLM mode.

    The production LangGraph assistant may only select these read-only lookups. This
    fallback deliberately makes no provider calls and never performs a side effect.
    """
    tool_names = [
        "get_trip_context",
        "get_flight_status",
        "get_connection_feasibility",
    ]
    relevant = [item for item in evidence if item.freshness_status == FreshnessStatus.FRESH]
    incident.tool_audit = [
        ToolAuditEvent(
            tool_name=name,
            arguments={"trip_id": str(incident.trip_id)},
            evidence_ids=[item.evidence_id for item in relevant],
        )
        for name in tool_names[:3]
    ]
    missing = [
        item.source_type for item in evidence if item.freshness_status != FreshnessStatus.FRESH
    ]
    evidence_ids = [item.evidence_id for item in evidence]
    incident.recommendation = Recommendation(
        incident_id=incident.incident_id,
        severity_explanation=(
            f"This trip is {incident.severity.value} risk based on the verified evidence shown. "
            "RouteShield has not made or requested any booking change."
        ),
        evidence_ids=evidence_ids,
        uncertainty="high" if missing else "medium",
        recommended_action=(
            "Review the current itinerary and provider evidence before "
            "approving any recovery action."
        ),
        traveler_message=(
            "Your travel manager is reviewing a possible disruption. "
            "No itinerary change has been made."
        ),
        manager_message=(
            "Review the evidence timestamps and decide whether a "
            "policy-eligible recovery option is needed."
        ),
        missing_information=missing,
    )
    incident.status = IncidentStatus.PENDING_APPROVAL
    incident.updated_at = datetime.now(UTC)
    return incident


def graph_traveler_context(profile: TravelerPreferenceProfile | None) -> dict[str, object] | None:
    """Return the allow-listed, confirmed profile fields that may influence recovery advice."""
    if not profile:
        return None
    return {
        "preferred_airports": profile.preferred_airports,
        "preferred_carriers": profile.preferred_carriers,
        "minimum_connection_minutes": profile.minimum_connection_minutes,
        "avoid_overnight_connections": profile.avoid_overnight_connections,
        "approved_ground_transport_preferences": profile.approved_ground_transport_preferences,
        "approved_accessibility_accommodations": profile.approved_accessibility_accommodations,
        "version": profile.version,
    }


def create_limited_visibility_incident(
    incident: Incident, evidence: list[EvidenceEnvelope]
) -> Incident:
    """Create a manager-review record without entering the ReAct tool loop."""
    unavailable = [
        item.source_type
        for item in evidence
        if item.freshness_status in {FreshnessStatus.STALE, FreshnessStatus.UNAVAILABLE}
    ]
    incident.recommendation = Recommendation(
        incident_id=incident.incident_id,
        severity_explanation=(
            "RouteShield has limited visibility because required sources are unavailable."
        ),
        evidence_ids=[item.evidence_id for item in evidence],
        uncertainty="high",
        recommended_action="Manually verify the itinerary before taking any recovery action.",
        traveler_message=(
            "Your travel manager is checking your itinerary. No itinerary change has been made."
        ),
        manager_message=(
            "Required source evidence is unavailable; verify the itinerary directly with providers."
        ),
        missing_information=unavailable,
    )
    incident.status = IncidentStatus.PENDING_APPROVAL
    incident.updated_at = datetime.now(UTC)
    return incident


async def run_graph_investigation(
    incident: Incident,
    assessment,
    evidence: list[EvidenceEnvelope],
    trip: Trip,
    source_health: SourceHealth,
) -> Incident:
    """Run the bounded investigation and project its interrupt payload onto an incident.

    Provider evidence has already been collected and scored before this function is called.
    The graph receives only normalized, tenant-scoped data and stops at the approval interrupt;
    approval and any external dispatch are handled later by explicit API operations.
    """
    thread_id = thread_id_for(
        tenant_id=incident.tenant_id, trip_id=incident.trip_id, incident_id=incident.incident_id
    )
    profile = (
        await repository.get_profile(incident.tenant_id, trip.traveler_id)
        if await control_enabled(incident.tenant_id, RuntimeControlName.MEMORY_READS_ENABLED)
        else None
    )
    playbooks = await repository.list_approved_playbooks(incident.tenant_id)
    llm_enabled = await control_enabled(incident.tenant_id, RuntimeControlName.LLM_ENABLED)
    state = await route_risk_graph.ainvoke(
        {
            "tenant_id": incident.tenant_id,
            "trip_id": str(incident.trip_id),
            "incident_id": str(incident.incident_id),
            "trip_context": trip.model_dump(mode="json"),
            "traveler_context": graph_traveler_context(profile),
            "playbook_context": [
                {
                    "playbook_id": str(playbook.playbook_id),
                    "name": playbook.name,
                    "version": playbook.version,
                    "guidance": playbook.guidance,
                }
                for playbook in playbooks
            ],
            "risk_assessment": assessment.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "source_health": source_health.model_dump(mode="json"),
            "react_iterations": 0,
            "tool_audit": [],
            "model_audit": [],
            "llm_enabled": llm_enabled,
            "tool_selection_attempted": False,
        },
        {"configurable": {"thread_id": thread_id}},
    )
    incident.tool_audit = [
        ToolAuditEvent.model_validate(item) for item in state.get("tool_audit", [])
    ]
    incident.model_audit = [
        ModelInvocationAudit.model_validate(item) for item in state.get("model_audit", [])
    ]
    await emit_event(
        event_type="memory_context.loaded",
        tenant_id=incident.tenant_id,
        trip_id=incident.trip_id,
        correlation_id=incident.correlation_id,
        details={
            "graph_thread_id": thread_id,
            "profile_loaded": profile is not None,
            "memory_context_version": profile.version if profile else None,
            "playbook_count": len(playbooks),
            "llm_enabled": llm_enabled,
        },
    )
    for tool_event in incident.tool_audit:
        await emit_event(
            event_type="tool.completed",
            tenant_id=incident.tenant_id,
            trip_id=incident.trip_id,
            correlation_id=incident.correlation_id,
            details={
                "graph_thread_id": thread_id,
                "tool_name": tool_event.tool_name,
                "outcome": tool_event.outcome,
                "evidence_count": len(tool_event.evidence_ids),
            },
        )
    for model_event in incident.model_audit:
        await emit_event(
            event_type="model.invocation",
            tenant_id=incident.tenant_id,
            trip_id=incident.trip_id,
            correlation_id=incident.correlation_id,
            details={
                "provider": model_event.provider,
                "model": model_event.model,
                "prompt_version": model_event.prompt_version,
                "invocation_type": model_event.invocation_type,
                "outcome": model_event.outcome,
                "token_usage_recorded": model_event.token_usage is not None,
                "cost_recorded": model_event.estimated_cost_usd is not None,
            },
        )
    raw_recommendation = state.get("recommendation")
    if isinstance(raw_recommendation, dict):
        recommendation_payload = dict(raw_recommendation)
        recommendation_payload["incident_id"] = incident.incident_id
        try:
            incident.recommendation = Recommendation.model_validate(recommendation_payload)
        except Exception:
            # A malformed graph/model result is never persisted as a recommendation.
            incident.recommendation = None
    if incident.recommendation:
        await emit_event(
            event_type="recommendation.generated",
            tenant_id=incident.tenant_id,
            trip_id=incident.trip_id,
            correlation_id=incident.correlation_id,
            details={
                "graph_thread_id": thread_id,
                "evidence_count": len(incident.recommendation.evidence_ids),
                "uncertainty": incident.recommendation.uncertainty,
                "requires_human_approval": incident.recommendation.requires_human_approval,
            },
        )
    raw_candidates = state.get("alternative_candidates")
    ranking = state.get("recovery_ranking")
    policy_decision = state.get("policy_decision")
    if isinstance(raw_candidates, list):
        try:
            candidate_set = RecoveryCandidateSet(
                incident_id=incident.incident_id,
                score_version=(
                    str(ranking.get("score_version", "deterministic-v1"))
                    if isinstance(ranking, dict)
                    else "deterministic-v1"
                ),
                policy_version=(
                    str(policy_decision.get("policy_version", recovery_policy.version))
                    if isinstance(policy_decision, dict)
                    else recovery_policy.version
                ),
                candidates=[EvaluatedCandidate.model_validate(item) for item in raw_candidates],
            )
        except Exception:
            # Candidate data is optional but must never be partially persisted.
            candidate_set = None
        if candidate_set:
            await repository.save_candidate_set(incident.tenant_id, candidate_set)
            await emit_event(
                event_type="recovery.candidate_set_created",
                tenant_id=incident.tenant_id,
                trip_id=incident.trip_id,
                correlation_id=incident.correlation_id,
                details={
                    "candidate_set_id": str(candidate_set.candidate_set_id),
                    "candidate_count": len(candidate_set.candidates),
                    "source": "graph",
                },
            )
            await emit_event(
                event_type="recovery.ranking_completed",
                tenant_id=incident.tenant_id,
                trip_id=incident.trip_id,
                correlation_id=incident.correlation_id,
                details={
                    "candidate_set_id": str(candidate_set.candidate_set_id),
                    "ranking_method": "deterministic",
                    "ranked_candidate_ids": (
                        ranking.get("ranked_candidate_ids", [])
                        if isinstance(ranking, dict)
                        else []
                    ),
                },
            )
    interrupt = state.get("__interrupt__", [])
    incident.graph_thread_id = thread_id
    if interrupt:
        incident.approval_payload = dict(interrupt[0].value)
    return incident


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "routeshield-api",
        "llm_enabled": str(controls.llm_enabled).lower(),
        "react_tool_calls_enabled": str(controls.react_tool_calls_enabled).lower(),
    }


@app.post("/v1/trips/import/csv")
async def validate_trip_csv(
    content: str = Body(media_type="text/csv"),
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> dict[str, object]:
    require_role(x_actor_role, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    content_bytes = content.encode("utf-8")
    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope="POST:/v1/trips/import/csv",
        payload={"content_sha256": content_sha256},
    )
    if replay:
        return replay.response_payload or {}
    upload_id = uuid4()
    try:
        object_key, stored_sha256 = await original_upload_store.put(
            tenant_id=tenant_id,
            upload_id=upload_id,
            content=content_bytes,
            content_type="text/csv",
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="original upload storage is unavailable",
        ) from exc
    upload = OriginalUploadRecord(
        original_upload_id=upload_id,
        tenant_id=tenant_id,
        source="csv",
        object_key=object_key,
        content_sha256=stored_sha256,
        content_size_bytes=len(content_bytes),
        content_type="text/csv",
    )
    await repository.save_original_upload(upload)
    try:
        rows = parse_csv_rows(content)
    except ValueError as exc:
        upload.status = OriginalUploadStatus.REJECTED
        upload.validation_errors = [str(exc)]
        await repository.save_original_upload(upload)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if any(row["tenant_id"] != tenant_id for row in rows):
        upload.status = OriginalUploadStatus.REJECTED
        upload.validation_errors = ["tenant mismatch"]
        await repository.save_original_upload(upload)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant mismatch")
    upload.status = OriginalUploadStatus.VALIDATED
    upload.validated_at = datetime.now(UTC)
    await repository.save_original_upload(upload)
    response_payload: dict[str, object] = {
        "validated_rows": len(rows),
        "original_upload_id": str(upload.original_upload_id),
        "status": upload.status.value,
    }
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=response_payload,
    )
    return response_payload


@app.post("/v1/webhooks/booking", status_code=status.HTTP_202_ACCEPTED)
async def booking_webhook(
    body: bytes = Body(),
    x_webhook_signature: str | None = Header(default=None),
    x_webhook_timestamp: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> dict[str, object]:
    secret = os.getenv("BOOKING_WEBHOOK_SECRET")
    if not secret or not x_webhook_signature or not x_webhook_timestamp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook credentials"
        )
    if not verify_webhook_signature(
        body=body,
        signature=x_webhook_signature,
        secret=secret,
        timestamp=x_webhook_timestamp,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook signature"
        )
    try:
        timestamp = parse_webhook_timestamp(x_webhook_timestamp)
        webhook = parse_booking_webhook(body, timestamp=timestamp)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if x_tenant_id and x_tenant_id != webhook.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant mismatch")

    trip_to_save: Trip | None = None
    material_change: str | None = None
    if webhook.event_type in {"itinerary.upsert", "itinerary.updated"}:
        raw_trip = webhook.data.get("trip")
        if not isinstance(raw_trip, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{webhook.event_type} requires a trip object",
            )
        try:
            trip_payload = TripCreate.model_validate(raw_trip)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{webhook.event_type} trip is invalid",
            ) from exc
        if trip_payload.tenant_id != webhook.tenant_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant mismatch")
        existing_trip = (
            await repository.get_trip(webhook.tenant_id, webhook.trip_id)
            if webhook.trip_id
            else None
        )
        if webhook.event_type == "itinerary.updated" and not webhook.trip_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="itinerary.updated requires trip_id",
            )
        if webhook.event_type == "itinerary.updated" and not existing_trip:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trip not found")
        trip_to_save = Trip(
            **trip_payload.model_dump(),
            trip_id=webhook.trip_id or uuid4(),
            created_at=existing_trip.created_at if existing_trip else datetime.now(UTC),
        )
        material_change = "itinerary_updated" if existing_trip else "itinerary_created"
    elif webhook.event_type == "itinerary.cancelled":
        if not webhook.trip_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="itinerary.cancelled requires trip_id",
            )
        existing_trip = await repository.get_trip(webhook.tenant_id, webhook.trip_id)
        if not existing_trip:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trip not found")
        trip_to_save = existing_trip.model_copy(update={"status": TripStatus.CANCELLED})
        material_change = "itinerary_cancelled"

    idempotency_key = f"booking-webhook:{webhook.message_id}"
    scope = "POST:/v1/webhooks/booking"
    request_hash, replay = await claim_mutation(
        tenant_id=webhook.tenant_id,
        idempotency_key=idempotency_key,
        scope=scope,
        payload=webhook.model_dump(mode="json"),
    )
    if replay:
        return replay.response_payload or {"status": "accepted"}

    event = await repository.record_event(
        EventRecord(
            event_type=f"booking_webhook.{webhook.event_type}",
            tenant_id=webhook.tenant_id,
            trip_id=trip_to_save.trip_id if trip_to_save else webhook.trip_id,
            correlation_id=uuid4(),
            idempotency_key=idempotency_key,
            details={
                "message_id": webhook.message_id,
                "occurred_at": webhook.occurred_at.isoformat(),
                "payload_keys": sorted(webhook.data),
                "material_change": material_change,
            },
        )
    )
    assessment_id = None
    if trip_to_save:
        await repository.save_trip(trip_to_save)
        if material_change == "itinerary_updated":
            # A new itinerary invalidates earlier flight and route evidence.
            await repository.save_evidence(trip_to_save.tenant_id, trip_to_save.trip_id, [])
        elif material_change == "itinerary_cancelled":
            current_evidence = await repository.get_evidence(
                trip_to_save.tenant_id, trip_to_save.trip_id
            )
            cancellation_evidence = EvidenceEnvelope(
                source_name="booking-webhook",
                source_type="flight_status",
                source_url_or_record_id=f"booking-webhook:{webhook.message_id}",
                normalized_payload={"risk_score": 100, "booking_status": "cancelled"},
            )
            await repository.save_evidence(
                trip_to_save.tenant_id,
                trip_to_save.trip_id,
                [
                    item for item in current_evidence if item.source_type != "flight_status"
                ]
                + [cancellation_evidence],
            )
        assessment = await assess_trip(
            trip_to_save.trip_id,
            x_tenant_id=trip_to_save.tenant_id,
            x_actor_role=UserRole.TENANT_ADMIN,
            x_actor_id="booking-webhook",
            idempotency_key=f"{idempotency_key}:assessment",
        )
        assessment_id = str(assessment.assessment.assessment_id)
    response_payload: dict[str, object] = {
        "status": "accepted",
        "message_id": webhook.message_id,
        "event_id": str(event.event_id),
        "trip_id": str(trip_to_save.trip_id) if trip_to_save else None,
        "assessment_id": assessment_id,
        "material_change": material_change,
    }
    await complete_mutation(
        tenant_id=webhook.tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_202_ACCEPTED,
        response_payload=response_payload,
    )
    return response_payload


@app.post("/v1/trips", response_model=Trip, status_code=status.HTTP_201_CREATED)
async def create_trip(
    payload: TripCreate,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> Trip:
    require_role(x_actor_role, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    if payload.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant mismatch")
    actor_id = require_actor_id(x_actor_id)
    trip_payload = payload.model_dump()
    if x_actor_role == UserRole.TRAVEL_MANAGER and actor_id:
        if payload.assigned_manager_id and payload.assigned_manager_id != actor_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="assignment mismatch")
        trip_payload["assigned_manager_id"] = actor_id
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope="POST:/v1/trips",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return Trip.model_validate(replay.response_payload)
    trip = await repository.create_trip(Trip(**trip_payload))
    await emit_event(
        event_type="trip.created", tenant_id=tenant_id, trip_id=trip.trip_id, correlation_id=uuid4()
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_201_CREATED,
        response_payload=trip.model_dump(mode="json"),
    )
    return trip


@app.get("/v1/runs/{run_id}/events", response_model=list[EventRecord])
async def run_events(
    run_id: str,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    limit: int = Query(default=100, ge=1, le=100),
) -> list[EventRecord]:
    """Return the audit trail for one incident run, within the caller's trip scope."""
    require_role(
        x_actor_role,
        UserRole.TRAVEL_MANAGER,
        UserRole.DUTY_OF_CARE,
        UserRole.TENANT_ADMIN,
    )
    tenant_id = require_tenant(x_tenant_id)
    actor_id = require_actor_id(x_actor_id)
    incidents = await repository.list_incidents(tenant_id)
    incident = next((item for item in incidents if str(item.correlation_id) == run_id), None)
    if not incident:
        return []
    trip = await repository.get_trip(tenant_id, incident.trip_id)
    if not trip:
        return []
    require_trip_access(
        trip=trip,
        x_actor_role=x_actor_role,
        x_actor_id=actor_id,
        allowed_roles=(UserRole.TRAVEL_MANAGER, UserRole.DUTY_OF_CARE, UserRole.TENANT_ADMIN),
    )
    if (
        x_actor_role == UserRole.DUTY_OF_CARE
        and incident.severity.value not in {"high", "critical"}
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="incident access denied")
    events = await repository.list_events(tenant_id)
    return [item for item in events if str(item.correlation_id) == run_id][-limit:]


@app.get("/v1/trips/{trip_id}/alternatives")
async def alternatives_for_trip(
    trip_id: UUID,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> list[dict[str, object]]:
    trip = await repository.get_trip(require_tenant(x_tenant_id), trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trip not found")
    require_trip_access(
        trip=trip,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(
            UserRole.TRAVELER,
            UserRole.TRAVEL_MANAGER,
            UserRole.DUTY_OF_CARE,
            UserRole.TENANT_ADMIN,
        ),
    )
    evaluated = [
        evaluate_policy_eligible_candidate(item, recovery_policy)
        for item in fixture_alternatives()
    ]
    return [item.model_dump(mode="json") for item in rank_eligible(evaluated)]


@app.post("/v1/incidents/{incident_id}/candidate-sets", response_model=RecoveryCandidateSet)
async def create_candidate_set(
    incident_id: UUID,
    payload: RecoveryCandidateSetCreate,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> RecoveryCandidateSet:
    require_role(x_actor_role, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    incident, _ = await require_incident_trip_access(
        tenant_id=tenant_id,
        incident_id=incident_id,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN),
    )
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"POST:/v1/incidents/{incident_id}/candidate-sets",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return RecoveryCandidateSet.model_validate(replay.response_payload)
    evaluated = [
        evaluate_policy_eligible_candidate(candidate, recovery_policy)
        for candidate in payload.candidates
    ]
    ranked = rank_eligible(evaluated)
    positions = {candidate.candidate_id: candidate.displayed_position for candidate in ranked}
    candidate_set = RecoveryCandidateSet(
        incident_id=incident_id,
        policy_version=recovery_policy.version,
        candidates=[
            candidate.model_copy(
                update={"displayed_position": positions.get(candidate.candidate_id)}
            )
            for candidate in evaluated
        ],
    )
    candidate_set = await repository.save_candidate_set(tenant_id, candidate_set)
    await emit_event(
        event_type="recovery.candidate_set_created",
        tenant_id=tenant_id,
        trip_id=incident.trip_id,
        correlation_id=incident.correlation_id,
        actor_id=require_actor_id(x_actor_id),
        details={
            "candidate_set_id": str(candidate_set.candidate_set_id),
            "candidate_count": len(candidate_set.candidates),
            "eligible_candidate_count": len(ranked),
            "policy_version": candidate_set.policy_version,
            "score_version": candidate_set.score_version,
        },
    )
    await emit_event(
        event_type="recovery.ranking_completed",
        tenant_id=tenant_id,
        trip_id=incident.trip_id,
        correlation_id=incident.correlation_id,
        actor_id=require_actor_id(x_actor_id),
        details={
            "candidate_set_id": str(candidate_set.candidate_set_id),
            "ranked_candidate_ids": [candidate.candidate_id for candidate in ranked],
            "ranking_method": "deterministic",
        },
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=candidate_set.model_dump(mode="json"),
    )
    return candidate_set


@app.get("/v1/incidents/{incident_id}/candidate-sets", response_model=list[RecoveryCandidateSet])
async def list_candidate_sets(
    incident_id: UUID,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> list[RecoveryCandidateSet]:
    require_role(
        x_actor_role,
        UserRole.TRAVEL_MANAGER,
        UserRole.DUTY_OF_CARE,
        UserRole.TENANT_ADMIN,
    )
    tenant_id = require_tenant(x_tenant_id)
    await require_incident_trip_access(
        tenant_id=tenant_id,
        incident_id=incident_id,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(UserRole.TRAVEL_MANAGER, UserRole.DUTY_OF_CARE, UserRole.TENANT_ADMIN),
    )
    candidate_sets = await repository.list_candidate_sets(tenant_id, incident_id)
    return [
        project_candidate_set_outcomes(
            candidate_set,
            await repository.list_candidate_outcomes(tenant_id, candidate_set.candidate_set_id),
        )
        for candidate_set in candidate_sets
    ]


@app.post(
    "/v1/incidents/{incident_id}/candidate-sets/{candidate_set_id}/candidates/{candidate_id}/outcomes",
    response_model=RecoveryCandidateSet,
)
async def record_candidate_outcome(
    incident_id: UUID,
    candidate_set_id: UUID,
    candidate_id: str,
    payload: RecoveryCandidateOutcomeUpdate,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> RecoveryCandidateSet:
    require_role(
        x_actor_role,
        UserRole.TRAVELER,
        UserRole.TRAVEL_MANAGER,
        UserRole.TENANT_ADMIN,
    )
    tenant_id = require_tenant(x_tenant_id)
    incident, _ = await require_incident_trip_access(
        tenant_id=tenant_id,
        incident_id=incident_id,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(UserRole.TRAVELER, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN),
    )
    candidate_set = await repository.get_candidate_set(tenant_id, incident_id, candidate_set_id)
    if not candidate_set:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="candidate set not found")
    outcomes = await repository.list_candidate_outcomes(tenant_id, candidate_set_id)
    candidate_set_view = project_candidate_set_outcomes(candidate_set, outcomes)
    candidate = next(
        (item for item in candidate_set_view.candidates if item.candidate_id == candidate_id), None
    )
    if not candidate:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="candidate not found")
    if x_actor_role == UserRole.TRAVELER and payload.state not in {
        RecoveryCandidateOutcomeState.VIEWED,
        RecoveryCandidateOutcomeState.SELECTED,
        RecoveryCandidateOutcomeState.REJECTED,
    }:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="travelers may only view, select, or reject a candidate",
        )
    if payload.manager_override_reason and x_actor_role not in {
        UserRole.TRAVEL_MANAGER,
        UserRole.TENANT_ADMIN,
    }:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only a manager can record an override",
        )
    if (
        payload.state == RecoveryCandidateOutcomeState.SELECTED
        and candidate_set_view.selected_candidate_id
        and candidate_set_view.selected_candidate_id != candidate_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a different candidate has already been selected",
        )
    if (
        payload.state == RecoveryCandidateOutcomeState.SELECTED
        and candidate.displayed_position not in {None, 1}
        and x_actor_role in {UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN}
        and not payload.manager_override_reason
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="selecting a non-top candidate requires a manager override reason",
        )
    if not transition_allowed(
        current_state=candidate.lifecycle_state, target_state=payload.state
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"cannot transition candidate from {candidate.lifecycle_state.value} "
                f"to {payload.state.value}"
            ),
        )
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=(
            f"POST:/v1/incidents/{incident_id}/candidate-sets/{candidate_set_id}/"
            f"candidates/{candidate_id}/outcomes"
        ),
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return RecoveryCandidateSet.model_validate(replay.response_payload)
    outcome = RecoveryCandidateOutcome(
        **payload.model_dump(),
        candidate_set_id=candidate_set_id,
        incident_id=incident_id,
        candidate_id=candidate_id,
        actor_id=require_actor_id(x_actor_id),
    )
    await repository.record_candidate_outcome(tenant_id, outcome)
    feedback_records: list[ManagerFeedback] = []
    if outcome.manager_override_reason:
        feedback_records.append(
            ManagerFeedback(
                tenant_id=tenant_id,
                incident_id=incident_id,
                candidate_set_id=candidate_set_id,
                candidate_id=candidate_id,
                feedback_type=ManagerFeedbackType.MANAGER_OVERRIDE,
                outcome_id=outcome.outcome_id,
                actor_id=outcome.actor_id,
                reason=outcome.manager_override_reason,
                details={"displayed_position": candidate.displayed_position},
            )
        )
    if outcome.state == RecoveryCandidateOutcomeState.COMPLETED:
        feedback_records.append(
            ManagerFeedback(
                tenant_id=tenant_id,
                incident_id=incident_id,
                candidate_set_id=candidate_set_id,
                candidate_id=candidate_id,
                feedback_type=ManagerFeedbackType.RECOVERY_OUTCOME,
                outcome_id=outcome.outcome_id,
                actor_id=outcome.actor_id,
                reason=outcome.reason,
                details={
                    "final_itinerary": outcome.final_itinerary or {},
                    "material_outcome": outcome.material_outcome or {},
                },
            )
        )
    for feedback in feedback_records:
        await repository.save_manager_feedback(feedback)
    candidate_set_view = project_candidate_set_outcomes(candidate_set, [*outcomes, outcome])
    await emit_event(
        event_type="recovery.outcome_recorded",
        tenant_id=tenant_id,
        trip_id=incident.trip_id,
        correlation_id=incident.correlation_id,
        actor_id=outcome.actor_id,
        details={
            "candidate_set_id": str(candidate_set_id),
            "candidate_id": candidate_id,
            "outcome_id": str(outcome.outcome_id),
            "state": outcome.state.value,
            "has_manager_override": bool(outcome.manager_override_reason),
            "has_material_outcome": bool(outcome.material_outcome),
            "manager_feedback_ids": [str(feedback.feedback_id) for feedback in feedback_records],
        },
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=candidate_set_view.model_dump(mode="json"),
    )
    return candidate_set_view


@app.get(
    "/v1/incidents/{incident_id}/manager-feedback", response_model=list[ManagerFeedback]
)
async def list_manager_feedback(
    incident_id: UUID,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> list[ManagerFeedback]:
    require_role(x_actor_role, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    await require_incident_trip_access(
        tenant_id=tenant_id,
        incident_id=incident_id,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN),
    )
    return await repository.list_manager_feedback(tenant_id, incident_id)


@app.get(
    "/v1/incidents/{incident_id}/actions", response_model=list[ActionDispatchRecord]
)
async def list_incident_action_dispatches(
    incident_id: UUID,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> list[ActionDispatchRecord]:
    require_role(x_actor_role, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    await require_incident_trip_access(
        tenant_id=tenant_id,
        incident_id=incident_id,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN),
    )
    return await repository.list_action_dispatches(tenant_id, incident_id)


@app.get("/v1/trips/{trip_id}", response_model=Trip)
async def get_trip(
    trip_id: UUID,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> Trip:
    trip = await repository.get_trip(require_tenant(x_tenant_id), trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trip not found")
    require_trip_access(
        trip=trip,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(
            UserRole.TRAVELER,
            UserRole.TRAVEL_MANAGER,
            UserRole.DUTY_OF_CARE,
            UserRole.TENANT_ADMIN,
        ),
    )
    return trip


@app.put("/v1/trips/{trip_id}/assignment", response_model=Trip)
async def assign_trip_manager(
    trip_id: UUID,
    payload: TripAssignmentRequest,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> Trip:
    """Tenant administrators explicitly grant a travel manager access to a trip."""
    require_role(x_actor_role, UserRole.TENANT_ADMIN)
    assignment_actor_id = require_actor_id(x_actor_id)
    tenant_id = require_tenant(x_tenant_id)
    trip = await repository.get_trip(tenant_id, trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trip not found")
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"PUT:/v1/trips/{trip_id}/assignment",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return Trip.model_validate(replay.response_payload)
    trip.assigned_manager_id = payload.manager_id
    await repository.save_trip(trip)
    await emit_event(
        event_type="trip.manager_assigned",
        tenant_id=tenant_id,
        trip_id=trip.trip_id,
        correlation_id=uuid4(),
        actor_id=assignment_actor_id,
        details={"assigned_manager_id": payload.manager_id},
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=trip.model_dump(mode="json"),
    )
    return trip


@app.put("/v1/trips/{trip_id}/evidence", status_code=status.HTTP_204_NO_CONTENT)
async def put_evidence(
    trip_id: UUID,
    payload: list[EvidenceEnvelope],
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> Response:
    tenant_id = require_tenant(x_tenant_id)
    trip = await repository.get_trip(tenant_id, trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trip not found")
    require_trip_access(
        trip=trip,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(UserRole.TRAVEL_MANAGER, UserRole.DUTY_OF_CARE, UserRole.TENANT_ADMIN),
    )
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"PUT:/v1/trips/{trip_id}/evidence",
        payload=[item.model_dump(mode="json") for item in payload],
    )
    if replay:
        return Response(status_code=replay.response_status_code or status.HTTP_204_NO_CONTENT)
    await repository.save_evidence(tenant_id, trip_id, payload)
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_204_NO_CONTENT,
        response_payload={},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/v1/trips/{trip_id}/assess", response_model=AssessmentResult)
async def assess_trip(
    trip_id: UUID,
    scenario: DemoScenario | None = None,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> AssessmentResult:
    """Perform one idempotent, evidence-backed assessment for an authorized trip.

    The sequence is intentionally linear: authorize, reuse or collect an evidence snapshot,
    calculate the deterministic score, choose a safe disposition, and persist any resulting
    incident. The graph is only invoked for elevated risk and never authorizes a booking.
    """
    tenant_id = require_tenant(x_tenant_id)
    trip = await repository.get_trip(tenant_id, trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trip not found")
    require_trip_access(
        trip=trip,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(UserRole.TRAVEL_MANAGER, UserRole.DUTY_OF_CARE, UserRole.TENANT_ADMIN),
    )
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"POST:/v1/trips/{trip_id}/assess",
        payload={"scenario": scenario.value if scenario else None},
    )
    if replay:
        return AssessmentResult.model_validate(replay.response_payload)
    assessment_correlation_id = uuid4()
    # Evidence is persisted as the assessment snapshot. A retry reuses it instead of making a
    # second provider call that could produce a different decision for the same request.
    evidence = await repository.get_evidence(tenant_id, trip_id)
    if not evidence:
        evidence = await collect_assessment_evidence(
            trip=trip, scenario=scenario, correlation_id=str(assessment_correlation_id)
        )
        await repository.save_evidence(tenant_id, trip_id, evidence)
    for item in evidence:
        await emit_event(
            event_type=(
                "provider.unavailable"
                if item.freshness_status in {FreshnessStatus.STALE, FreshnessStatus.UNAVAILABLE}
                else "provider.evidence_observed"
            ),
            tenant_id=tenant_id,
            trip_id=trip_id,
            correlation_id=assessment_correlation_id,
            details={
                "source_name": item.source_name,
                "source_type": item.source_type,
                "freshness_status": item.freshness_status.value,
                "provider_latency_ms": item.provider_latency_ms,
                "error_code": item.error_code,
            },
        )
    factors, unknown = get_scoring_input(evidence, trip)
    assessment = calculate_risk(
        trip_id=trip_id,
        factors=factors,
        evidence_ids=[item.evidence_id for item in evidence],
        unknown_factors=unknown,
    )
    await repository.save_assessment(tenant_id, assessment)
    health = source_health_for(evidence)
    disposition = disposition_for(assessment, health)
    incident_id = None
    if disposition in {AssessmentDisposition.INVESTIGATE, AssessmentDisposition.NEEDS_HUMAN_REVIEW}:
        incident = Incident(
            tenant_id=tenant_id,
            trip_id=trip_id,
            assessment_id=assessment.assessment_id,
            severity=assessment.severity,
            correlation_id=assessment_correlation_id,
        )
        # Source-health review uses a limited-visibility recommendation. Only healthy elevated
        # cases can enter the bounded read-only graph, and the tenant kill switch remains in force.
        use_react = disposition == AssessmentDisposition.INVESTIGATE and await control_enabled(
            tenant_id, RuntimeControlName.REACT_TOOL_CALLS_ENABLED
        )
        incident = (
            investigate(incident, evidence)
            if use_react
            else create_limited_visibility_incident(incident, evidence)
        )
        if use_react:
            incident = await run_graph_investigation(incident, assessment, evidence, trip, health)
        await repository.save_incident(incident)
        incident_id = incident.incident_id
        await emit_event(
            event_type="incident.created",
            tenant_id=tenant_id,
            trip_id=trip_id,
            correlation_id=incident.correlation_id,
        )
        await emit_event(
            event_type="approval.requested",
            tenant_id=tenant_id,
            trip_id=trip_id,
            correlation_id=incident.correlation_id,
        )
    await emit_event(
        event_type="assessment.completed",
        tenant_id=tenant_id,
        trip_id=trip_id,
        correlation_id=assessment_correlation_id,
        details={
            "assessment_id": str(assessment.assessment_id),
            "risk_score": assessment.risk_score,
            "severity": assessment.severity.value,
            "disposition": disposition.value,
            "unknown_factor_count": len(assessment.unknown_factors),
        },
    )
    result = AssessmentResult(
        assessment=assessment,
        evidence=evidence,
        source_health=health,
        disposition=disposition,
        incident_id=incident_id,
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=result.model_dump(mode="json"),
    )
    return result


@app.get("/v1/incidents", response_model=list[Incident])
async def list_incidents(
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[Incident]:
    require_role(
        x_actor_role,
        UserRole.TRAVEL_MANAGER,
        UserRole.DUTY_OF_CARE,
        UserRole.TENANT_ADMIN,
    )
    tenant_id = require_tenant(x_tenant_id)
    actor_id = require_actor_id(x_actor_id)
    incidents = await repository.list_incidents(tenant_id)
    if x_actor_role == UserRole.TRAVEL_MANAGER and actor_id:
        assigned_incidents: list[Incident] = []
        for incident in incidents:
            trip = await repository.get_trip(tenant_id, incident.trip_id)
            if trip and trip.assigned_manager_id == actor_id:
                assigned_incidents.append(incident)
        incidents = assigned_incidents
    if x_actor_role == UserRole.DUTY_OF_CARE:
        incidents = [item for item in incidents if item.severity.value in {"high", "critical"}]
    severity_order = {"critical": 4, "high": 3, "watch": 2, "low": 1}
    return sorted(
        incidents,
        key=lambda item: (severity_order[item.severity.value], item.created_at),
        reverse=True,
    )[offset : offset + limit]


@app.get("/v1/incidents/{incident_id}", response_model=Incident)
async def get_incident(
    incident_id: UUID,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> Incident:
    require_role(
        x_actor_role,
        UserRole.TRAVEL_MANAGER,
        UserRole.DUTY_OF_CARE,
        UserRole.TENANT_ADMIN,
    )
    tenant_id = require_tenant(x_tenant_id)
    incident = await repository.get_incident(tenant_id, incident_id)
    if not incident:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")
    trip = await repository.get_trip(tenant_id, incident.trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trip not found")
    require_trip_access(
        trip=trip,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(UserRole.TRAVEL_MANAGER, UserRole.DUTY_OF_CARE, UserRole.TENANT_ADMIN),
    )
    if (
        x_actor_role == UserRole.DUTY_OF_CARE
        and incident.severity.value not in {"high", "critical"}
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="incident access denied")
    return incident


@app.get("/v1/travelers/{traveler_id}/incidents", response_model=list[TravelerIncidentView])
async def list_traveler_incidents(
    traveler_id: str,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> list[TravelerIncidentView]:
    """Return a traveler-safe view without manager rationale or unapproved actions."""
    require_role(x_actor_role, UserRole.TRAVELER)
    actor_id = require_actor_id(x_actor_id)
    if actor_id and actor_id != traveler_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="incident access denied")
    tenant_id = require_tenant(x_tenant_id)
    views: list[TravelerIncidentView] = []
    for incident in await repository.list_incidents(tenant_id):
        trip = await repository.get_trip(tenant_id, incident.trip_id)
        if not trip or trip.traveler_id != traveler_id:
            continue
        guidance = None
        if incident.status == IncidentStatus.APPROVED and incident.recommendation:
            guidance = incident.recommendation.traveler_message
        views.append(
            TravelerIncidentView(
                incident_id=incident.incident_id,
                trip_id=incident.trip_id,
                severity=incident.severity,
                status=incident.status,
                created_at=incident.created_at,
                updated_at=incident.updated_at,
                approved_guidance=guidance,
            )
        )
    return views


async def decide_incident(
    incident_id: UUID, payload: ApprovalRequest, tenant_id: str
) -> ApprovalRecord:
    incident = await repository.get_incident(tenant_id, incident_id)
    if not incident:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")
    if incident.status != IncidentStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="incident is not awaiting approval"
        )
    action_dispatch_status = (
        "suppressed"
        if payload.final_action_payload
        and not await control_enabled(tenant_id, RuntimeControlName.APPROVAL_ACTIONS_ENABLED)
        else "pending"
        if payload.final_action_payload
        else "not_requested"
    )
    approval = await repository.save_approval(
        ApprovalRecord(
            incident_id=incident_id,
            action_dispatch_status=action_dispatch_status,
            **payload.model_dump(),
        )
    )
    action: ActionDispatchRecord | None = None
    if payload.final_action_payload and action_dispatch_status == "pending":
        action = ActionDispatchRecord(
            action_dispatch_id=uuid5(
                NAMESPACE_URL, f"routeshield:approved-action:{approval.approval_id}"
            ),
            tenant_id=tenant_id,
            incident_id=incident_id,
            approval_id=approval.approval_id,
            action_payload=payload.final_action_payload,
            idempotency_key=f"approved-action:{approval.approval_id}",
        )
        await repository.save_action_dispatch(action)
        await emit_event(
            event_type="action.queued",
            tenant_id=tenant_id,
            trip_id=incident.trip_id,
            correlation_id=incident.correlation_id,
            actor_id=payload.actor_id,
            details={
                "action_dispatch_id": str(action.action_dispatch_id),
                "idempotency_key": action.idempotency_key,
            },
        )
    incident.status = (
        IncidentStatus.APPROVED
        if payload.decision == ApprovalDecision.APPROVE
        else IncidentStatus.REJECTED
    )
    incident.updated_at = approval.decided_at
    await repository.save_incident(incident)
    if incident.graph_thread_id:
        resume_payload: dict[str, str] = {
            "decision": payload.decision.value,
            "reason": payload.reason,
        }
        if action:
            resume_payload.update(
                {
                    "action_dispatch_id": str(action.action_dispatch_id),
                    "action_idempotency_key": action.idempotency_key,
                }
            )
        await route_risk_graph.ainvoke(
            Command(resume=resume_payload),
            {"configurable": {"thread_id": incident.graph_thread_id}},
        )
    await emit_event(
        event_type="approval.completed",
        tenant_id=tenant_id,
        trip_id=incident.trip_id,
        correlation_id=incident.correlation_id,
        actor_id=payload.actor_id,
        details={
            "decision": payload.decision.value,
            "final_action_payload": payload.final_action_payload,
            "action_dispatch_status": action_dispatch_status,
        },
    )
    return approval


@app.post("/v1/incidents/{incident_id}/approve", response_model=ApprovalRecord)
async def approve_incident(
    incident_id: UUID,
    payload: ApprovalRequest,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> ApprovalRecord:
    require_role(x_actor_role, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    require_actor_matches_payload(x_actor_id=x_actor_id, payload_actor_id=payload.actor_id)
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key is required"
        )
    if payload.decision != ApprovalDecision.APPROVE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="decision must be approve"
        )
    tenant_id = require_tenant(x_tenant_id)
    await require_incident_trip_access(
        tenant_id=tenant_id,
        incident_id=incident_id,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN),
    )
    request_hash, replay = await claim_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"POST:/v1/incidents/{incident_id}/approve",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return ApprovalRecord.model_validate(replay.response_payload)
    approval = await decide_incident(incident_id, payload, tenant_id)
    await complete_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=approval.model_dump(mode="json"),
    )
    return approval


@app.post("/v1/incidents/{incident_id}/reject", response_model=ApprovalRecord)
async def reject_incident(
    incident_id: UUID,
    payload: ApprovalRequest,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> ApprovalRecord:
    require_role(x_actor_role, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    require_actor_matches_payload(x_actor_id=x_actor_id, payload_actor_id=payload.actor_id)
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key is required"
        )
    if payload.decision != ApprovalDecision.REJECT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="decision must be reject"
        )
    tenant_id = require_tenant(x_tenant_id)
    await require_incident_trip_access(
        tenant_id=tenant_id,
        incident_id=incident_id,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN),
    )
    request_hash, replay = await claim_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"POST:/v1/incidents/{incident_id}/reject",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return ApprovalRecord.model_validate(replay.response_payload)
    approval = await decide_incident(incident_id, payload, tenant_id)
    await complete_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=approval.model_dump(mode="json"),
    )
    return approval


@app.post("/v1/incidents/{incident_id}/refresh", response_model=AssessmentResult)
async def refresh_incident(
    incident_id: UUID,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> AssessmentResult:
    require_role(
        x_actor_role,
        UserRole.TRAVEL_MANAGER,
        UserRole.DUTY_OF_CARE,
        UserRole.TENANT_ADMIN,
    )
    tenant_id = require_tenant(x_tenant_id)
    incident = await repository.get_incident(tenant_id, incident_id)
    if not incident:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"POST:/v1/incidents/{incident_id}/refresh",
        payload={"incident_id": str(incident_id)},
    )
    if replay:
        return AssessmentResult.model_validate(replay.response_payload)
    assessment_key = f"{idempotency_key}:assessment" if idempotency_key else None
    result = await assess_trip(
        incident.trip_id,
        x_tenant_id=tenant_id,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        idempotency_key=assessment_key,
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=result.model_dump(mode="json"),
    )
    return result


@app.get("/v1/travelers/{traveler_id}/preferences", response_model=TravelerPreferenceProfile | None)
async def get_preferences(
    traveler_id: str,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> TravelerPreferenceProfile | None:
    tenant_id = require_tenant(x_tenant_id)
    await require_memory_reads_enabled(tenant_id)
    require_role(x_actor_role, UserRole.TRAVELER, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    actor_id = require_actor_id(x_actor_id)
    if x_actor_role == UserRole.TRAVELER and actor_id and actor_id != traveler_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="preference access denied"
        )
    return await repository.get_profile(tenant_id, traveler_id)


@app.put("/v1/travelers/{traveler_id}/preferences", response_model=TravelerPreferenceProfile)
async def update_preferences(
    traveler_id: str,
    payload: TravelerPreferenceUpdate,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> TravelerPreferenceProfile:
    """Write an explicit settings change without routing it through an LLM proposal."""
    require_role(x_actor_role, UserRole.TRAVELER, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    actor_id = require_actor_id(x_actor_id)
    if x_actor_role == UserRole.TRAVELER and actor_id and actor_id != traveler_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="preference access denied"
        )
    if not await control_enabled(tenant_id, RuntimeControlName.MEMORY_WRITES_ENABLED):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="memory writes disabled"
        )
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"PUT:/v1/travelers/{traveler_id}/preferences",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return TravelerPreferenceProfile.model_validate(replay.response_payload)
    existing = await repository.get_profile(tenant_id, traveler_id)
    baseline = (
        existing.model_dump()
        if existing
        else {
            "tenant_id": tenant_id,
            "traveler_id": traveler_id,
            "consent_version": payload.consent_version,
        }
    )
    changed = payload.model_dump(exclude_none=True)
    profile = TravelerPreferenceProfile(
        **{
            **baseline,
            **changed,
            "version": (existing.version + 1 if existing else 1),
            "updated_at": datetime.now(UTC),
        }
    )
    await repository.save_profile(profile)
    await repository.record_memory_audit(
        MemoryAuditEvent(
            tenant_id=tenant_id,
            traveler_id=traveler_id,
            action="updated",
            actor_id=actor_id or "local-compat-settings",
            details={
                "changed_fields": sorted(changed),
                "previous_version": existing.version if existing else None,
                "profile_version": profile.version,
                "source": "explicit_settings_api",
            },
        )
    )
    await emit_event(
        event_type="memory_update.explicit",
        tenant_id=tenant_id,
        trip_id=None,
        correlation_id=uuid4(),
        actor_id=actor_id,
        details={"traveler_id": traveler_id, "profile_version": profile.version},
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=profile.model_dump(mode="json"),
    )
    return profile


@app.post(
    "/v1/memory/proposals",
    response_model=MemoryUpdateProposal,
    status_code=status.HTTP_201_CREATED,
)
async def create_memory_proposal(
    payload: MemoryUpdateProposal,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> MemoryUpdateProposal:
    require_role(x_actor_role, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    require_actor_matches_payload(x_actor_id=x_actor_id, payload_actor_id=payload.actor_id)
    tenant_id = require_tenant(x_tenant_id)
    if not await control_enabled(tenant_id, RuntimeControlName.MEMORY_WRITES_ENABLED):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="memory writes disabled"
        )
    if payload.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant mismatch")
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope="POST:/v1/memory/proposals",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return MemoryUpdateProposal.model_validate(replay.response_payload)
    proposal = await repository.save_memory_proposal(payload)
    await repository.record_memory_audit(
        MemoryAuditEvent(
            tenant_id=tenant_id,
            traveler_id=proposal.traveler_id,
            proposal_id=proposal.proposal_id,
            action="proposed",
            actor_id=proposal.actor_id,
            details={
                "changed_fields": sorted(proposal.patch),
                "consent_required": proposal.consent_required,
            },
        )
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_201_CREATED,
        response_payload=proposal.model_dump(mode="json"),
    )
    return proposal


@app.post("/v1/memory/proposals/{proposal_id}/confirm", response_model=TravelerPreferenceProfile)
async def confirm_memory_proposal(
    proposal_id: UUID,
    actor_id: str,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> TravelerPreferenceProfile:
    require_role(x_actor_role, UserRole.TRAVELER)
    confirmed_actor_id = require_actor_matches_payload(
        x_actor_id=x_actor_id, payload_actor_id=actor_id
    )
    tenant_id = require_tenant(x_tenant_id)
    if not await control_enabled(tenant_id, RuntimeControlName.MEMORY_WRITES_ENABLED):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="memory writes disabled"
        )
    proposal = await repository.get_memory_proposal(tenant_id, proposal_id)
    if not proposal:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="memory proposal not found"
        )
    if confirmed_actor_id != proposal.traveler_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="consent actor mismatch")
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"POST:/v1/memory/proposals/{proposal_id}/confirm",
        payload={"actor_id": actor_id},
    )
    if replay:
        return TravelerPreferenceProfile.model_validate(replay.response_payload)
    if proposal.status != MemoryProposalStatus.PENDING_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="proposal is already resolved"
        )
    existing = await repository.get_profile(tenant_id, proposal.traveler_id)
    baseline = (
        existing.model_dump()
        if existing
        else {
            "tenant_id": tenant_id,
            "traveler_id": proposal.traveler_id,
            "consent_version": "pending",
        }
    )
    allowed = set(TravelerPreferenceProfile.model_fields) - {
        "tenant_id",
        "traveler_id",
        "version",
        "updated_at",
    }
    forbidden = set(proposal.patch) - allowed
    if forbidden:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="proposal contains restricted fields",
        )
    profile = TravelerPreferenceProfile(
        **{**baseline, **proposal.patch, "version": (existing.version + 1 if existing else 1)}
    )
    proposal.status = MemoryProposalStatus.CONFIRMED
    proposal.resolved_at = datetime.now(UTC)
    await repository.save_profile(profile)
    await repository.save_memory_proposal(proposal)
    await repository.record_memory_audit(
        MemoryAuditEvent(
            tenant_id=tenant_id,
            traveler_id=proposal.traveler_id,
            proposal_id=proposal.proposal_id,
            action="confirmed",
            actor_id=confirmed_actor_id,
            details={"changed_fields": sorted(proposal.patch), "profile_version": profile.version},
        )
    )
    await emit_event(
        event_type="memory_update.confirmed",
        tenant_id=tenant_id,
        trip_id=uuid4(),
        correlation_id=uuid4(),
        actor_id=confirmed_actor_id,
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=profile.model_dump(mode="json"),
    )
    return profile


@app.post(
    "/v1/privacy/legal-holds",
    response_model=LegalHoldRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_legal_hold(
    payload: LegalHoldRecord,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> LegalHoldRecord:
    """Create a scoped hold before retention or a deletion request can erase data."""
    require_role(x_actor_role, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    require_actor_matches_payload(x_actor_id=x_actor_id, payload_actor_id=payload.created_by)
    if payload.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant mismatch")
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope="POST:/v1/privacy/legal-holds",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return LegalHoldRecord.model_validate(replay.response_payload)
    hold = await repository.save_legal_hold(payload)
    await emit_event(
        event_type="privacy.legal_hold_created",
        tenant_id=tenant_id,
        trip_id=hold.trip_id,
        correlation_id=uuid4(),
        actor_id=hold.created_by,
        details={"legal_hold_id": str(hold.legal_hold_id), "scope": hold.scope.value},
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_201_CREATED,
        response_payload=hold.model_dump(mode="json"),
    )
    return hold


@app.post("/v1/privacy/legal-holds/{legal_hold_id}/release", response_model=LegalHoldRecord)
async def release_legal_hold(
    legal_hold_id: UUID,
    payload: LegalHoldRelease,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> LegalHoldRecord:
    require_role(x_actor_role, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    actor_id = require_actor_id(x_actor_id)
    if not actor_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="actor required")
    hold = await repository.get_legal_hold(tenant_id, legal_hold_id)
    if not hold:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="legal hold not found")
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"POST:/v1/privacy/legal-holds/{legal_hold_id}/release",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return LegalHoldRecord.model_validate(replay.response_payload)
    if hold.released_at:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="legal hold is released")
    released = hold.model_copy(
        update={
            "released_at": datetime.now(UTC),
            "released_by": actor_id,
            "release_reason": payload.reason,
        }
    )
    await repository.save_legal_hold(released)
    await emit_event(
        event_type="privacy.legal_hold_released",
        tenant_id=tenant_id,
        trip_id=released.trip_id,
        correlation_id=uuid4(),
        actor_id=actor_id,
        details={"legal_hold_id": str(released.legal_hold_id), "scope": released.scope.value},
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=released.model_dump(mode="json"),
    )
    return released


@app.get("/v1/privacy/legal-holds", response_model=list[LegalHoldRecord])
async def list_legal_holds(
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
) -> list[LegalHoldRecord]:
    require_role(x_actor_role, UserRole.TENANT_ADMIN)
    return await repository.list_legal_holds(require_tenant(x_tenant_id))


@app.post(
    "/v1/privacy/deletion-requests",
    response_model=DeletionRequest,
    status_code=status.HTTP_201_CREATED,
)
async def create_deletion_request(
    payload: DeletionRequest,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> DeletionRequest:
    require_role(x_actor_role, UserRole.TRAVELER, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    requester = require_actor_matches_payload(
        x_actor_id=x_actor_id, payload_actor_id=payload.requested_by
    )
    if payload.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant mismatch")
    if x_actor_role == UserRole.TRAVELER and requester != payload.traveler_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="deletion request denied")
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope="POST:/v1/privacy/deletion-requests",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return DeletionRequest.model_validate(replay.response_payload)
    request = payload.model_copy(
        update={
            "status": DeletionRequestStatus.PENDING,
            "completed_at": None,
            "blocked_by_hold_ids": [],
            "failure_code": None,
        }
    )
    stored = await repository.save_deletion_request(request)
    await emit_event(
        event_type="privacy.deletion_requested",
        tenant_id=tenant_id,
        trip_id=None,
        correlation_id=uuid4(),
        actor_id=requester,
        details={
            "deletion_request_id": str(stored.deletion_request_id),
            "scope": stored.scope.value,
        },
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_201_CREATED,
        response_payload=stored.model_dump(mode="json"),
    )
    return stored


@app.get("/v1/privacy/deletion-requests", response_model=list[DeletionRequest])
async def list_deletion_requests(
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> list[DeletionRequest]:
    require_role(x_actor_role, UserRole.TRAVELER, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    actor_id = require_actor_id(x_actor_id)
    traveler_id = actor_id if x_actor_role == UserRole.TRAVELER else None
    return await repository.list_deletion_requests(tenant_id, traveler_id=traveler_id)


@app.delete("/v1/travelers/{traveler_id}/preferences", status_code=status.HTTP_204_NO_CONTENT)
async def delete_traveler_memory(
    traveler_id: str,
    actor_id: str,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> Response:
    """Delete consented preference memory while retaining a minimal audit record."""
    require_role(x_actor_role, UserRole.TRAVELER, UserRole.TENANT_ADMIN)
    deletion_actor_id = require_actor_matches_payload(
        x_actor_id=x_actor_id, payload_actor_id=actor_id
    )
    if x_actor_role == UserRole.TRAVELER and deletion_actor_id != traveler_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="memory deletion denied")
    tenant_id = require_tenant(x_tenant_id)
    holds = await repository.active_legal_holds_for_traveler(tenant_id, traveler_id)
    if holds:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="memory deletion is blocked by an active legal hold",
        )
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"DELETE:/v1/travelers/{traveler_id}/preferences",
        payload={"actor_id": actor_id},
    )
    if replay:
        return Response(status_code=replay.response_status_code or status.HTTP_204_NO_CONTENT)
    deleted = await repository.delete_traveler_memory(tenant_id, traveler_id)
    await repository.record_memory_audit(
        MemoryAuditEvent(
            tenant_id=tenant_id,
            traveler_id=traveler_id,
            action="deleted",
            actor_id=deletion_actor_id,
            details={"deleted_record_counts": deleted},
        )
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_204_NO_CONTENT,
        response_payload={},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/v1/travelers/{traveler_id}/memory-audit", response_model=list[MemoryAuditEvent])
async def list_memory_audit(
    traveler_id: str,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
) -> list[MemoryAuditEvent]:
    tenant_id = require_tenant(x_tenant_id)
    await require_memory_reads_enabled(tenant_id)
    require_role(x_actor_role, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    return await repository.list_memory_audit(tenant_id, traveler_id)


@app.get("/v1/incidents/{incident_id}/notification-preview", response_model=NotificationCreate)
async def preview_incident_notification(
    incident_id: UUID,
    channel: str = Query(default="in_app", pattern="^(email|sms|push|in_app)$"),
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> NotificationCreate:
    tenant_id = require_tenant(x_tenant_id)
    incident = await repository.get_incident(tenant_id, incident_id)
    if not incident:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="incident not found")
    trip = await repository.get_trip(tenant_id, incident.trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trip not found")
    require_trip_access(
        trip=trip,
        x_actor_role=x_actor_role,
        x_actor_id=x_actor_id,
        allowed_roles=(
            UserRole.TRAVELER,
            UserRole.TRAVEL_MANAGER,
            UserRole.DUTY_OF_CARE,
            UserRole.TENANT_ADMIN,
        ),
    )
    message = (
        incident.recommendation.traveler_message
        if incident.recommendation
        else (
            "Your travel manager is reviewing a possible disruption. "
            "No itinerary change has been made."
        )
    )
    return NotificationCreate(
        incident_id=incident_id,
        traveler_id=trip.traveler_id,
        channel=channel,
        recipient_reference=f"traveler:{trip.traveler_id}",
        subject=f"RouteShield travel update: {incident.severity.value} risk",
        body=message,
    )


@app.post(
    "/v1/notifications",
    response_model=NotificationRecord,
    status_code=status.HTTP_201_CREATED,
)
async def queue_notification(
    payload: NotificationCreate,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> NotificationRecord:
    require_role(x_actor_role, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    if payload.incident_id:
        _, trip = await require_incident_trip_access(
            tenant_id=tenant_id,
            incident_id=payload.incident_id,
            x_actor_role=x_actor_role,
            x_actor_id=x_actor_id,
            allowed_roles=(UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN),
        )
        if payload.traveler_id != trip.traveler_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="notification traveler does not match incident trip",
            )
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope="POST:/v1/notifications",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return NotificationRecord.model_validate(replay.response_payload)
    notification = NotificationRecord(tenant_id=tenant_id, **payload.model_dump())
    await repository.save_notification(notification)
    incident = (
        await repository.get_incident(tenant_id, notification.incident_id)
        if notification.incident_id
        else None
    )
    await emit_event(
        event_type="notification.queued",
        tenant_id=tenant_id,
        trip_id=incident.trip_id if incident else None,
        correlation_id=incident.correlation_id if incident else uuid4(),
        actor_id=x_actor_id,
        details={
            "notification_id": str(notification.notification_id),
            "channel": notification.channel,
        },
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_201_CREATED,
        response_payload=notification.model_dump(mode="json"),
    )
    return notification


@app.get("/v1/notifications", response_model=list[NotificationRecord])
async def list_notifications(
    notification_status: NotificationStatus | None = Query(default=None, alias="status"),
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> list[NotificationRecord]:
    require_role(x_actor_role, UserRole.TRAVELER, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    actor_id = require_actor_id(x_actor_id)
    statuses = {notification_status} if notification_status else None
    notifications = await repository.list_notifications(require_tenant(x_tenant_id), statuses)
    if x_actor_role == UserRole.TRAVELER and actor_id:
        return [item for item in notifications if item.traveler_id == actor_id]
    if x_actor_role == UserRole.TRAVEL_MANAGER and actor_id:
        scoped_notifications: list[NotificationRecord] = []
        tenant_id = require_tenant(x_tenant_id)
        for notification in notifications:
            if not notification.incident_id:
                continue
            incident = await repository.get_incident(tenant_id, notification.incident_id)
            trip = await repository.get_trip(tenant_id, incident.trip_id) if incident else None
            if trip and trip.assigned_manager_id == actor_id:
                scoped_notifications.append(notification)
        return scoped_notifications
    return notifications


@app.post("/v1/notifications/dispatch", response_model=list[NotificationRecord])
async def dispatch_notifications(
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> list[NotificationRecord]:
    """Process due queue entries; only the safe in-app channel is configured by default."""
    require_role(x_actor_role, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    if not (
        await control_enabled(tenant_id, RuntimeControlName.NOTIFICATIONS_ENABLED)
        and await control_enabled(tenant_id, RuntimeControlName.TENANT_AUTOMATION_ENABLED)
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="notification delivery automation is disabled",
        )
    worker_id = require_actor_id(x_actor_id) or "tenant-admin-notification-dispatcher"
    pending = await repository.claim_due_notifications(
        tenant_id=tenant_id,
        worker_id=worker_id,
        claim_ttl=timedelta(minutes=5),
    )
    sender = InAppNotificationSender()
    delivered: list[NotificationRecord] = []
    for item in pending:
        outcome = await deliver_notification(
            repository=repository, notification=item, sender=sender
        )
        incident = (
            await repository.get_incident(tenant_id, outcome.incident_id)
            if outcome.incident_id
            else None
        )
        await emit_event(
            event_type=f"notification.{outcome.status.value}",
            tenant_id=tenant_id,
            trip_id=incident.trip_id if incident else None,
            correlation_id=incident.correlation_id if incident else uuid4(),
            actor_id=worker_id,
            details={
                "notification_id": str(outcome.notification_id),
                "channel": outcome.channel,
                "attempt_count": outcome.attempt_count,
                "error_code": outcome.last_error_code,
            },
        )
        delivered.append(outcome)
    return delivered


@app.post("/v1/internal/notifications/dispatch", response_model=list[NotificationRecord])
async def dispatch_due_notifications_job() -> list[NotificationRecord]:
    """Pub/Sub push target; Cloud Run IAM restricts this endpoint to the worker SA."""
    sender = InAppNotificationSender()
    delivered: list[NotificationRecord] = []
    for item in await repository.claim_due_notifications(
        tenant_id=None,
        worker_id="internal-notification-dispatcher",
        claim_ttl=timedelta(minutes=5),
    ):
        if not (
            await control_enabled(item.tenant_id, RuntimeControlName.NOTIFICATIONS_ENABLED)
            and await control_enabled(item.tenant_id, RuntimeControlName.TENANT_AUTOMATION_ENABLED)
        ):
            continue
        outcome = await deliver_notification(
            repository=repository, notification=item, sender=sender
        )
        incident = (
            await repository.get_incident(item.tenant_id, outcome.incident_id)
            if outcome.incident_id
            else None
        )
        await emit_event(
            event_type=f"notification.{outcome.status.value}",
            tenant_id=item.tenant_id,
            trip_id=incident.trip_id if incident else None,
            correlation_id=incident.correlation_id if incident else uuid4(),
            actor_id="internal-notification-dispatcher",
            details={
                "notification_id": str(outcome.notification_id),
                "channel": outcome.channel,
                "attempt_count": outcome.attempt_count,
                "error_code": outcome.last_error_code,
            },
        )
        delivered.append(outcome)
    return delivered


@app.post("/v1/internal/actions/dispatch", response_model=list[ActionDispatchRecord])
async def dispatch_approved_actions_job() -> list[ActionDispatchRecord]:
    """Claim approved-action outbox entries; no booking adapter is enabled by default."""
    sender = UnconfiguredActionSender()
    dispatched: list[ActionDispatchRecord] = []
    for action in await repository.claim_due_action_dispatches(
        tenant_id=None,
        worker_id="internal-approved-action-dispatcher",
        claim_ttl=timedelta(minutes=5),
    ):
        if not (
            await control_enabled(action.tenant_id, RuntimeControlName.APPROVAL_ACTIONS_ENABLED)
            and await control_enabled(
                action.tenant_id, RuntimeControlName.TENANT_AUTOMATION_ENABLED
            )
        ):
            continue
        outcome = await dispatch_approved_action(
            repository=repository, action=action, sender=sender
        )
        incident = await repository.get_incident(action.tenant_id, action.incident_id)
        await emit_event(
            event_type=f"action.{outcome.status.value}",
            tenant_id=action.tenant_id,
            trip_id=incident.trip_id if incident else None,
            correlation_id=incident.correlation_id if incident else uuid4(),
            actor_id="internal-approved-action-dispatcher",
            details={
                "action_dispatch_id": str(outcome.action_dispatch_id),
                "attempt_count": outcome.attempt_count,
                "error_code": outcome.last_error_code,
            },
        )
        dispatched.append(outcome)
    return dispatched


@app.post("/v1/internal/assessments/due")
async def assess_due_trips_job() -> dict[str, int]:
    """Cloud Scheduler/PubSub target; Cloud Run IAM restricts invocations to the worker SA."""
    return await process_due_assessments()


@app.get(
    "/v1/notifications/{notification_id}/attempts",
    response_model=list[NotificationAttempt],
)
async def notification_attempt_history(
    notification_id: UUID,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
) -> list[NotificationAttempt]:
    require_role(x_actor_role, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    notification = await repository.get_notification(tenant_id, notification_id)
    if not notification:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="notification not found")
    if x_actor_role == UserRole.TRAVEL_MANAGER:
        if not notification.incident_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="notification access denied"
            )
        await require_incident_trip_access(
            tenant_id=tenant_id,
            incident_id=notification.incident_id,
            x_actor_role=x_actor_role,
            x_actor_id=x_actor_id,
            allowed_roles=(UserRole.TRAVEL_MANAGER,),
        )
    return await repository.list_notification_attempts(tenant_id, notification_id)


@app.post("/v1/notifications/{notification_id}/acknowledge", response_model=NotificationRecord)
async def acknowledge_notification(
    notification_id: UUID,
    actor_id: str,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> NotificationRecord:
    require_role(x_actor_role, UserRole.TRAVELER, UserRole.TRAVEL_MANAGER, UserRole.TENANT_ADMIN)
    acknowledgement_actor_id = require_actor_matches_payload(
        x_actor_id=x_actor_id, payload_actor_id=actor_id
    )
    tenant_id = require_tenant(x_tenant_id)
    notification = await repository.get_notification(tenant_id, notification_id)
    if not notification:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="notification not found")
    if x_actor_role == UserRole.TRAVELER and acknowledgement_actor_id != notification.traveler_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="notification access denied"
        )
    if x_actor_role == UserRole.TRAVEL_MANAGER:
        if not notification.incident_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="notification access denied"
            )
        await require_incident_trip_access(
            tenant_id=tenant_id,
            incident_id=notification.incident_id,
            x_actor_role=x_actor_role,
            x_actor_id=x_actor_id,
            allowed_roles=(UserRole.TRAVEL_MANAGER,),
        )
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"POST:/v1/notifications/{notification_id}/acknowledge",
        payload={"actor_id": actor_id},
    )
    if replay:
        return NotificationRecord.model_validate(replay.response_payload)
    if notification.status not in {NotificationStatus.DELIVERED, NotificationStatus.ACKNOWLEDGED}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="notification has not been delivered"
        )
    if notification.status == NotificationStatus.DELIVERED:
        notification.status = NotificationStatus.ACKNOWLEDGED
        notification.acknowledged_at = datetime.now(UTC)
        notification.acknowledgement_actor_id = acknowledgement_actor_id
        notification.updated_at = notification.acknowledged_at
        await repository.save_notification(notification)
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=notification.model_dump(mode="json"),
    )
    return notification


@app.post(
    "/v1/governance/changes",
    response_model=ChangeRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_change_record(
    payload: ChangeRecord,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> ChangeRecord:
    require_role(x_actor_role, UserRole.TENANT_ADMIN)
    require_actor_matches_payload(x_actor_id=x_actor_id, payload_actor_id=payload.requested_by)
    tenant_id = require_tenant(x_tenant_id)
    if payload.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant mismatch")
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope="POST:/v1/governance/changes",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return ChangeRecord.model_validate(replay.response_payload)
    record = await repository.save_change_record(payload)
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_201_CREATED,
        response_payload=record.model_dump(mode="json"),
    )
    return record


@app.get("/v1/governance/changes", response_model=list[ChangeRecord])
async def list_change_records(
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
) -> list[ChangeRecord]:
    require_role(x_actor_role, UserRole.TENANT_ADMIN)
    return await repository.list_change_records(require_tenant(x_tenant_id))


@app.put(
    "/v1/governance/runtime-controls/{control_name}", response_model=RuntimeControlChange
)
async def set_runtime_control(
    control_name: RuntimeControlName,
    payload: RuntimeControlUpdate,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> RuntimeControlChange:
    """Apply an expiring tenant control override and retain its full audit history."""
    require_role(x_actor_role, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    if payload.expires_at and payload.expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="control expiry must be in the future",
        )
    actor_id = require_actor_id(x_actor_id) or "local-compat-tenant-admin"
    previous_enabled = await control_enabled(tenant_id, control_name)
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope=f"PUT:/v1/governance/runtime-controls/{control_name.value}",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return RuntimeControlChange.model_validate(replay.response_payload)
    record = RuntimeControlChange(
        tenant_id=tenant_id,
        control_name=control_name,
        previous_enabled=previous_enabled,
        actor_id=actor_id,
        **payload.model_dump(),
    )
    await repository.save_runtime_control_change(record)
    await emit_event(
        event_type="runtime_control.changed",
        tenant_id=tenant_id,
        trip_id=None,
        correlation_id=uuid4(),
        actor_id=actor_id,
        details={
            "scope": record.scope,
            "control_name": control_name.value,
            "reason": record.reason,
            "previous_enabled": previous_enabled,
            "enabled": record.enabled,
            "review_at": record.review_at.isoformat() if record.review_at else None,
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        },
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=record.model_dump(mode="json"),
    )
    return record


@app.get("/v1/governance/runtime-controls", response_model=list[RuntimeControlChange])
async def list_runtime_control_changes(
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
) -> list[RuntimeControlChange]:
    require_role(x_actor_role, UserRole.TENANT_ADMIN)
    return await repository.list_runtime_control_changes(require_tenant(x_tenant_id))


@app.put(
    "/v1/platform/runtime-controls/{control_name}", response_model=PlatformRuntimeControlChange
)
async def set_platform_runtime_control(
    control_name: RuntimeControlName,
    payload: RuntimeControlUpdate,
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> PlatformRuntimeControlChange:
    """Set a platform default without accepting a tenant identifier or exposing tenant data."""
    require_role(x_actor_role, UserRole.PLATFORM_ADMIN)
    if payload.expires_at and payload.expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="control expiry must be in the future",
        )
    actor_id = require_actor_id(x_actor_id) or "local-compat-platform-admin"
    current = await repository.get_platform_control_override(control_name)
    previous_enabled = current.enabled if current else default_control_enabled(control_name)
    request_hash, replay = await claim_optional_mutation(
        tenant_id="platform-control-plane",
        idempotency_key=idempotency_key,
        scope=f"PUT:/v1/platform/runtime-controls/{control_name.value}",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return PlatformRuntimeControlChange.model_validate(replay.response_payload)
    record = PlatformRuntimeControlChange(
        control_name=control_name,
        previous_enabled=previous_enabled,
        actor_id=actor_id,
        **payload.model_dump(),
    )
    await repository.save_platform_control_change(record)
    await emit_event(
        event_type="platform_runtime_control.changed",
        tenant_id="platform-control-plane",
        trip_id=None,
        correlation_id=uuid4(),
        actor_id=actor_id,
        details={
            "scope": "platform",
            "control_name": control_name.value,
            "reason": record.reason,
            "previous_enabled": previous_enabled,
            "enabled": record.enabled,
            "review_at": record.review_at.isoformat() if record.review_at else None,
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        },
    )
    await complete_optional_mutation(
        tenant_id="platform-control-plane",
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_200_OK,
        response_payload=record.model_dump(mode="json"),
    )
    return record


@app.get("/v1/platform/runtime-controls", response_model=list[PlatformRuntimeControlChange])
async def list_platform_runtime_controls(
    x_actor_role: UserRole | None = Header(default=None),
) -> list[PlatformRuntimeControlChange]:
    require_role(x_actor_role, UserRole.PLATFORM_ADMIN)
    return await repository.list_platform_control_changes()


@app.post(
    "/v1/governance/playbooks",
    response_model=TenantPlaybook,
    status_code=status.HTTP_201_CREATED,
)
async def publish_tenant_playbook(
    payload: TenantPlaybookCreate,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> TenantPlaybook:
    """Publish a tenant-admin-approved, immutable procedural guidance version."""
    require_role(x_actor_role, UserRole.TENANT_ADMIN)
    tenant_id = require_tenant(x_tenant_id)
    actor_id = require_actor_id(x_actor_id)
    if not actor_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="an administrator actor identity is required",
        )
    playbook_id = uuid5(
        NAMESPACE_URL, f"routeshield:playbook:{tenant_id}:{payload.name}:{payload.version}"
    )
    existing = await repository.get_playbook(tenant_id, playbook_id)
    if existing:
        if existing.guidance != payload.guidance:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="an immutable playbook already exists for this name and version",
            )
        return existing
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope="POST:/v1/governance/playbooks",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return TenantPlaybook.model_validate(replay.response_payload)
    playbook = TenantPlaybook(
        playbook_id=playbook_id,
        tenant_id=tenant_id,
        approved_by=actor_id,
        **payload.model_dump(),
    )
    stored = await repository.save_playbook(playbook)
    if stored.guidance != playbook.guidance:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="an immutable playbook already exists for this name and version",
        )
    await emit_event(
        event_type="playbook.published",
        tenant_id=tenant_id,
        trip_id=None,
        correlation_id=uuid4(),
        actor_id=actor_id,
        details={
            "playbook_id": str(stored.playbook_id),
            "name": stored.name,
            "version": stored.version,
        },
    )
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_201_CREATED,
        response_payload=stored.model_dump(mode="json"),
    )
    return stored


@app.get("/v1/governance/playbooks", response_model=list[TenantPlaybook])
async def list_tenant_playbooks(
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
) -> list[TenantPlaybook]:
    require_role(
        x_actor_role,
        UserRole.TRAVEL_MANAGER,
        UserRole.DUTY_OF_CARE,
        UserRole.TENANT_ADMIN,
    )
    return await repository.list_approved_playbooks(require_tenant(x_tenant_id))


@app.post(
    "/v1/governance/providers",
    response_model=ProviderOnboardingRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_provider_onboarding(
    payload: ProviderOnboardingRecord,
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
    x_actor_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> ProviderOnboardingRecord:
    require_role(x_actor_role, UserRole.TENANT_ADMIN)
    require_actor_matches_payload(x_actor_id=x_actor_id, payload_actor_id=payload.owner_id)
    tenant_id = require_tenant(x_tenant_id)
    if payload.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant mismatch")
    request_hash, replay = await claim_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        scope="POST:/v1/governance/providers",
        payload=payload.model_dump(mode="json"),
    )
    if replay:
        return ProviderOnboardingRecord.model_validate(replay.response_payload)
    record = await repository.save_provider_onboarding(payload)
    await complete_optional_mutation(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status_code=status.HTTP_201_CREATED,
        response_payload=record.model_dump(mode="json"),
    )
    return record


@app.get("/v1/governance/providers", response_model=list[ProviderOnboardingRecord])
async def list_provider_onboarding(
    x_tenant_id: str | None = Header(default=None),
    x_actor_role: UserRole | None = Header(default=None),
) -> list[ProviderOnboardingRecord]:
    require_role(x_actor_role, UserRole.TENANT_ADMIN)
    return await repository.list_provider_onboarding(require_tenant(x_tenant_id))


@app.post("/v1/internal/retention/run")
async def run_retention_job() -> dict[str, int]:
    """Cloud Scheduler invokes this on the IAM-protected Cloud Run service.

    The route intentionally accepts no tenant or payload: deployed ingress is
    restricted to the Scheduler service account and the retention period comes
    only from service configuration, never from an untrusted request.
    """
    result = await run_retention(repository, retention_days=controls.retention_days)
    await repository.record_event(
        EventRecord(
            event_type="retention.completed",
            tenant_id="system",
            trip_id=None,
            correlation_id=uuid4(),
            idempotency_key=f"retention:{datetime.now(UTC).date().isoformat()}",
            details={"deleted_record_counts": result, "retention_days": controls.retention_days},
        )
    )
    return result


@app.post("/v1/internal/privacy/deletion-requests/process")
async def process_privacy_deletion_requests_job() -> dict[str, int]:
    """Process pending DSAR requests with legal-hold checks before every erase."""
    processed = await process_due_deletion_requests(repository)
    summary = {"completed": 0, "blocked_by_legal_hold": 0, "failed": 0}
    for request, deleted in processed:
        summary[request.status.value] = summary.get(request.status.value, 0) + 1
        await emit_event(
            event_type=f"privacy.deletion_{request.status.value}",
            tenant_id=request.tenant_id,
            trip_id=None,
            correlation_id=uuid4(),
            actor_id=request.requested_by,
            details={
                "deletion_request_id": str(request.deletion_request_id),
                "scope": request.scope.value,
                "deleted_record_counts": deleted,
            },
        )
    return summary


app.mount("/console", StaticFiles(directory="apps/web", html=True), name="console")
