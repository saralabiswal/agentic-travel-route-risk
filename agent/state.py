"""JSON-serializable state and stable thread identifiers for the RouteShield graph."""

from __future__ import annotations

from typing import NotRequired, TypedDict
from uuid import UUID


class RouteRiskState(TypedDict):
    tenant_id: str
    trip_id: str
    incident_id: str
    trip_context: NotRequired[dict[str, object]]
    traveler_context: NotRequired[dict[str, object] | None]
    memory_context: NotRequired[dict[str, object] | None]
    memory_context_version: NotRequired[int | None]
    playbook_context: NotRequired[list[dict[str, object]]]
    approved_playbooks: NotRequired[list[dict[str, object]]]
    risk_assessment: dict[str, object]
    evidence: list[dict[str, object]]
    source_health: dict[str, object]
    react_iterations: int
    tool_audit: list[dict[str, object]]
    model_audit: NotRequired[list[dict[str, object]]]
    llm_enabled: NotRequired[bool]
    tool_selection_attempted: NotRequired[bool]
    tool_selection_mode: NotRequired[str]
    pending_tool_call: NotRequired[dict[str, object] | None]
    alternative_candidates: NotRequired[list[dict[str, object]]]
    eligible_alternatives: NotRequired[list[dict[str, object]]]
    recovery_ranking: NotRequired[dict[str, object] | None]
    policy_decision: NotRequired[dict[str, object] | None]
    policy_gate: NotRequired[dict[str, object] | None]
    recommendation: NotRequired[dict[str, object] | None]
    approval: NotRequired[dict[str, object] | None]
    dispatched_actions: NotRequired[list[dict[str, object]]]
    final_status: NotRequired[str]


def thread_id_for(*, tenant_id: str, trip_id: UUID | str, incident_id: UUID | str) -> str:
    """Return the tenant-scoped LangGraph checkpoint key required by the PRD."""
    return f"tenant:{tenant_id}:trip:{trip_id}:incident:{incident_id}"
