"""Bounded RouteShield V2 LangGraph workflow.

This module intentionally delays LangGraph imports so the V1 API can still run
without optional graph dependencies.  Production must compile this graph with a
PostgreSQL checkpointer; InMemorySaver is allowed only for local tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from agent.state import RouteRiskState
from domain.policies import (
    CorporateTravelPolicy,
    apply_traveler_minimum_connection,
    evaluate_policy_eligible_candidate,
)
from domain.recovery import EvaluatedCandidate, rank_eligible
from tools.alternatives import fixture_alternatives
from tools.openai_provider import READ_ONLY_TOOL_NAMES, OpenAIRecommendationProvider

READ_ONLY_TOOLS = READ_ONLY_TOOL_NAMES
MAX_REACT_ITERATIONS = 3


class GraphDependencyUnavailable(RuntimeError):
    """Raised only when a caller tries to run V2 without installing LangGraph."""


def _langgraph() -> tuple[Any, Any, Any, Any, Any]:
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt
    except ModuleNotFoundError as exc:
        raise GraphDependencyUnavailable(
            "Install the V2 dependency with `uv add langgraph` before running the graph."
        ) from exc
    return END, START, StateGraph, InMemorySaver, interrupt


def _severity(state: RouteRiskState) -> str:
    return str(state["risk_assessment"].get("severity", "low"))


def route_by_severity(
    state: RouteRiskState,
) -> Literal["load_memory_context", "approval_gate", "finalize"]:
    unavailable = state["source_health"].get("core_sources_unavailable", [])
    # Insufficient core evidence takes the short path to human review. Do not ask a model to
    # compensate for a provider outage, because that could turn missing data into false certainty.
    if isinstance(unavailable, list) and len(unavailable) >= 2:
        return "approval_gate"
    if (
        _severity(state) in {"high", "critical"}
        and isinstance(unavailable, list)
        and "flight_status" in unavailable
    ):
        return "approval_gate"
    if _severity(state) in {"high", "critical"}:
        return "load_memory_context"
    return "finalize"


def _next_tool(state: RouteRiskState) -> str | None:
    if state["react_iterations"] >= MAX_REACT_ITERATIONS:
        return None
    evidence_types = {str(item.get("source_type")) for item in state["evidence"]}
    already_called = {
        str(item.get("tool_name"))
        for item in state["tool_audit"]
        if isinstance(item, dict)
    }
    # The deterministic sequence is used after an optional model selection. It gives a useful,
    # repeatable audit trail and cannot exceed MAX_REACT_ITERATIONS.
    preferred = [
        ("flight_status", "get_flight_status"),
        ("connection_feasibility", "get_connection_feasibility"),
        ("airport_weather", "get_airport_weather"),
    ]
    for source_type, tool_name in preferred:
        if source_type in evidence_types and tool_name not in already_called:
            return tool_name
    return None


def _recommendation(state: RouteRiskState) -> dict[str, object]:
    evidence_ids = [str(item["evidence_id"]) for item in state["evidence"] if "evidence_id" in item]
    unavailable = state["source_health"].get("core_sources_unavailable", [])
    return {
        "severity_explanation": f"Deterministic assessment severity is {_severity(state)}.",
        "evidence_ids": evidence_ids,
        "uncertainty": "high" if unavailable else "medium",
        "recommended_action": "Have a travel manager review before any itinerary action.",
        "traveler_message": (
            "Your travel manager is reviewing a possible disruption. "
            "No itinerary change has been made."
        ),
        "manager_message": "Review the cited evidence before approving any itinerary action.",
        "requires_human_approval": True,
        "missing_information": unavailable if isinstance(unavailable, list) else [],
    }


def build_graph(
    *,
    checkpointer: Any | None = None,
    recommendation_provider: OpenAIRecommendationProvider | None = None,
) -> Any:
    """Compile the V2 workflow with a local checkpointer unless one is supplied.

    The graph's tool node validates the selected read-only lookup against the
    request-scoped evidence snapshot and records evidence references. Provider
    adapters remain server-side and collect that snapshot before graph execution.
    """
    end, start, state_graph, in_memory_saver, interrupt = _langgraph()
    recommendation_provider = recommendation_provider or OpenAIRecommendationProvider()

    def load_memory_context(state: RouteRiskState) -> dict[str, object]:
        """Load only the backend-scoped, confirmed preference snapshot into graph state."""
        supplied = state.get("traveler_context")
        allowed_fields = {
            "preferred_airports",
            "preferred_carriers",
            "minimum_connection_minutes",
            "avoid_overnight_connections",
            "approved_ground_transport_preferences",
            "approved_accessibility_accommodations",
            "version",
        }
        memory_context = (
            {key: value for key, value in supplied.items() if key in allowed_fields}
            if isinstance(supplied, dict)
            else None
        )
        supplied_playbooks = state.get("playbook_context", [])
        approved_playbooks = [
            {
                "playbook_id": item["playbook_id"],
                "name": item["name"],
                "version": item["version"],
                "guidance": item["guidance"],
            }
            for item in supplied_playbooks
            if isinstance(item, dict)
            and all(
                isinstance(item.get(key), str)
                for key in {"playbook_id", "name", "version", "guidance"}
            )
        ]
        version = memory_context.get("version") if memory_context else None
        audit = list(state["tool_audit"])
        audit.append(
            {
                "tool_name": "load_memory_context",
                "arguments": {"scope": "tenant_traveler_profile"},
                "evidence_ids": [],
                "outcome": (
                    "profile_and_playbooks_loaded"
                    if memory_context and approved_playbooks
                    else "profile_loaded"
                    if memory_context
                    else "playbooks_loaded"
                    if approved_playbooks
                    else "no_confirmed_profile_or_playbook"
                ),
            }
        )
        return {
            "memory_context": memory_context,
            "memory_context_version": version if isinstance(version, int) else None,
            "approved_playbooks": approved_playbooks,
            "tool_audit": audit,
        }

    def react_assistant(state: RouteRiskState) -> dict[str, object]:
        """Allow one model-planned lookup, then keep any follow-up bounded and server-owned.

        A single model tool-selection call plus the final recommendation call keeps an
        assessment within the PRD's two-completion budget.  Later lookups are a
        deterministic fallback that can fill the remaining bounded evidence checks.
        """
        model_selection_enabled = recommendation_provider.available_for(
            enabled=bool(state.get("llm_enabled", False))
        )
        if not state.get("tool_selection_attempted", False) and model_selection_enabled:
            audit = list(state["tool_audit"])
            model_audit = list(state.get("model_audit", []))
            input_summary = {
                "severity": _severity(state),
                "evidence_count": len(state["evidence"]),
                "core_sources_unavailable": state["source_health"].get(
                    "core_sources_unavailable", []
                ),
            }
            try:
                selection = recommendation_provider.select_tool(
                    trip_id=state["trip_id"],
                    risk_assessment=state["risk_assessment"],
                    evidence=state["evidence"],
                    source_health=state["source_health"],
                    enabled=True,
                )
            except Exception:
                # A provider/model failure must never block the deterministic, cited
                # fallback path or expose model/provider details to a client.
                audit.append(
                    {
                        "tool_name": "model_tool_selection",
                        "arguments": {},
                        "evidence_ids": [],
                        "outcome": "model_selection_failed_fallback_used",
                    }
                )
                model_audit.append(
                    {
                        **recommendation_provider.audit_metadata(
                            invocation_type="tool_selection", outcome="failed_fallback_used"
                        ),
                        "input_summary": input_summary,
                    }
                )
                return {
                    "tool_selection_attempted": True,
                    "tool_selection_mode": "deterministic_fallback",
                    "tool_audit": audit,
                    "model_audit": model_audit,
                }
            audit.append(
                {
                    "tool_name": "model_tool_selection",
                    "arguments": {"tool_requested": selection.name if selection else None},
                    "evidence_ids": [],
                    "outcome": "model_selected_tool" if selection else "model_selected_no_tool",
                }
            )
            model_audit.append(
                {
                    **recommendation_provider.audit_metadata(
                        invocation_type="tool_selection",
                        outcome="tool_selected" if selection else "no_tool_selected",
                    ),
                    "input_summary": input_summary,
                }
            )
            if selection:
                return {
                    "tool_selection_attempted": True,
                    "tool_selection_mode": "model_selected",
                    "tool_audit": audit,
                    "model_audit": model_audit,
                    "pending_tool_call": {
                        "name": selection.name,
                        "arguments": selection.arguments,
                    },
                }
            return {
                "tool_selection_attempted": True,
                "tool_selection_mode": "model_selected_no_tool",
                "tool_audit": audit,
                "model_audit": model_audit,
                "pending_tool_call": None,
                "recommendation": _recommendation(state),
            }
        tool_name = _next_tool(state)
        if tool_name is None:
            return {"pending_tool_call": None, "recommendation": _recommendation(state)}
        return {
            "tool_selection_mode": "deterministic_follow_up",
            "pending_tool_call": {
                "name": tool_name,
                "arguments": {"trip_id": state["trip_id"]},
            }
        }

    def route_after_assistant(state: RouteRiskState) -> Literal["tools", "recommendation"]:
        return "tools" if state.get("pending_tool_call") else "recommendation"

    def run_read_only_tool(state: RouteRiskState) -> dict[str, object]:
        call = state.get("pending_tool_call")
        if not isinstance(call, dict) or call.get("name") not in READ_ONLY_TOOLS:
            return {"pending_tool_call": None, "recommendation": _recommendation(state)}
        arguments = call.get("arguments")
        if not isinstance(arguments, dict) or arguments.get("trip_id") != state["trip_id"]:
            return {"pending_tool_call": None, "recommendation": _recommendation(state)}
        # This node deliberately does not call a provider. It validates that a bounded lookup is
        # relevant to the request-scoped evidence snapshot, then records the cited evidence IDs.
        # Providers are invoked by the API before this graph starts.
        audit = list(state["tool_audit"])
        audit.append(
            {
                "tool_name": call["name"],
                "arguments": arguments,
                "evidence_ids": [
                    str(item["evidence_id"]) for item in state["evidence"] if "evidence_id" in item
                ],
                "outcome": (
                    "model_selected_evidence_snapshot"
                    if state.get("tool_selection_mode") == "model_selected"
                    else "deterministic_evidence_snapshot"
                ),
            }
        )
        return {
            "react_iterations": state["react_iterations"] + 1,
            "tool_audit": audit,
            "pending_tool_call": None,
        }

    def validate_recommendation(state: RouteRiskState) -> dict[str, object]:
        recommendation = state.get("recommendation") or _recommendation(state)
        model_audit = list(state.get("model_audit", []))
        if (
            recommendation_provider.available_for(enabled=bool(state.get("llm_enabled", False)))
            and _severity(state) in {"high", "critical"}
        ):
            # The assessment can make one model tool-selection call and one final
            # recommendation call.  Do not silently retry here: that would exceed
            # the two-completion budget and turn a provider failure into a cost risk.
            try:
                recommendation = recommendation_provider.recommend(
                    incident_id=UUID(state["incident_id"]),
                    risk_assessment=state["risk_assessment"],
                    evidence=state["evidence"],
                    enabled=True,
                ).model_dump(mode="json")
                model_audit.append(
                    {
                        **recommendation_provider.audit_metadata(
                            invocation_type="recommendation", outcome="succeeded"
                        ),
                        "input_summary": {
                            "severity": _severity(state),
                            "evidence_count": len(state["evidence"]),
                        },
                    }
                )
            except Exception:
                recommendation = _recommendation(state)
                recommendation["uncertainty"] = "high"
                model_audit.append(
                    {
                        **recommendation_provider.audit_metadata(
                            invocation_type="recommendation", outcome="failed_fallback_used"
                        ),
                        "input_summary": {
                            "severity": _severity(state),
                            "evidence_count": len(state["evidence"]),
                        },
                    }
                )
        known_ids = {
            str(item["evidence_id"]) for item in state["evidence"] if "evidence_id" in item
        }
        cited_ids = recommendation.get("evidence_ids", [])
        # A model recommendation is usable only when every citation belongs to this assessment.
        # Invalid citations fall back to the deterministic recommendation instead of being repaired.
        if not isinstance(cited_ids, list) or not set(cited_ids).issubset(known_ids):
            recommendation = _recommendation(state)
            recommendation["uncertainty"] = "high"
        return {"recommendation": recommendation, "model_audit": model_audit}

    def verify_alternative_eligibility(state: RouteRiskState) -> dict[str, object]:
        """Evaluate every tool-returned option before any ranking or LLM display."""
        policy = CorporateTravelPolicy()
        memory_context = state.get("memory_context") or {}
        minimum_connection = memory_context.get("minimum_connection_minutes")
        if not isinstance(minimum_connection, int) or isinstance(minimum_connection, bool):
            minimum_connection = None
        evaluated = [
            apply_traveler_minimum_connection(
                evaluate_policy_eligible_candidate(candidate, policy),
                minimum_connection_minutes=minimum_connection,
            )
            for candidate in fixture_alternatives()
        ]
        audit = list(state["tool_audit"])
        audit.append(
            {
                "tool_name": "find_alternative_flights",
                "arguments": {"trip_id": state["trip_id"]},
                "evidence_ids": [
                    str(item["evidence_id"])
                    for item in state["evidence"]
                    if "evidence_id" in item
                ],
                "outcome": "policy_eligibility_evaluated",
            }
        )
        return {
            "alternative_candidates": [item.model_dump(mode="json") for item in evaluated],
            "eligible_alternatives": [],
            "policy_decision": {
                "policy_version": policy.version,
                "eligible_candidate_ids": [
                    item.candidate_id for item in evaluated if item.eligible
                ],
                "traveler_minimum_connection_minutes": minimum_connection,
                "memory_context_version": state.get("memory_context_version"),
                "approved_playbooks": state.get("approved_playbooks", []),
            },
            "tool_audit": audit,
        }

    def rank_eligible_recovery_options(state: RouteRiskState) -> dict[str, object]:
        """Apply the deterministic fallback order outside the assistant's control."""
        candidates = [
            EvaluatedCandidate.model_validate(item)
            for item in state.get("alternative_candidates", [])
        ]
        ranked = rank_eligible(candidates)
        displayed_positions = {
            candidate.candidate_id: candidate.displayed_position for candidate in ranked
        }
        all_candidates = [
            candidate.model_copy(
                update={"displayed_position": displayed_positions.get(candidate.candidate_id)}
            )
            for candidate in candidates
        ]
        return {
            "alternative_candidates": [
                item.model_dump(mode="json") for item in all_candidates
            ],
            "eligible_alternatives": [item.model_dump(mode="json") for item in ranked],
            "recovery_ranking": {
                "ranking_method": "deterministic",
                "score_version": "deterministic-v1",
                "ranked_candidate_ids": [item.candidate_id for item in ranked],
                "fallback_reason": "learned_ranker_not_enabled",
            },
        }

    def attach_recovery_ranking(state: RouteRiskState) -> dict[str, object]:
        """The assistant may explain ordering but cannot supply or modify it."""
        recommendation = dict(state.get("recommendation") or _recommendation(state))
        ranking = state.get("recovery_ranking") or {}
        ranked_ids = ranking.get("ranked_candidate_ids", [])
        if not isinstance(ranked_ids, list):
            ranked_ids = []
        recommendation["ranked_alternative_ids"] = ranked_ids
        recommendation["manager_message"] = (
            f"{recommendation['manager_message']} "
            f"{len(ranked_ids)} policy-eligible alternatives were ranked by the deterministic "
            "recovery score."
        )
        return {"recommendation": recommendation}

    def policy_gate(state: RouteRiskState) -> dict[str, object]:
        """Persist the policy decision before a human can approve any action request."""
        policy = state.get("policy_decision") or {}
        eligible_ids = policy.get("eligible_candidate_ids", []) if isinstance(policy, dict) else []
        if not isinstance(eligible_ids, list):
            eligible_ids = []
        return {
            "policy_gate": {
                "status": "human_approval_required",
                "eligible_candidate_ids": eligible_ids,
                "policy_version": (
                    policy.get("policy_version") if isinstance(policy, dict) else None
                ),
            }
        }

    def approval_gate(state: RouteRiskState) -> dict[str, object]:
        decision = interrupt(
            {
                "incident_id": state["incident_id"],
                "original_itinerary": state.get("trip_context"),
                "risk_assessment": state["risk_assessment"],
                "evidence": state["evidence"],
                "policy_result": state.get("policy_decision"),
                "policy_gate": state.get("policy_gate"),
                "eligible_alternatives": state.get("eligible_alternatives", []),
                "recovery_ranking": state.get("recovery_ranking"),
                "memory_context_version": state.get("memory_context_version"),
                "approved_playbooks": state.get("approved_playbooks", []),
                "recommendation": state.get("recommendation") or _recommendation(state),
                "tool_audit": state["tool_audit"],
                "proposed_external_action_payload": {},
            }
        )
        if not isinstance(decision, dict) or decision.get("decision") not in {"approve", "reject"}:
            return {"approval": {"decision": "reject", "reason": "invalid approval payload"}}
        return {"approval": decision}

    def queue_approved_action(state: RouteRiskState) -> dict[str, object]:
        """Checkpoint the API-created outbox identity; this node never contacts a provider."""
        approval = state.get("approval") or {}
        if not isinstance(approval, dict):
            return {"dispatched_actions": []}
        action_dispatch_id = approval.get("action_dispatch_id")
        idempotency_key = approval.get("action_idempotency_key")
        if approval.get("decision") != "approve" or not action_dispatch_id or not idempotency_key:
            return {"dispatched_actions": []}
        return {
            "dispatched_actions": [
                {
                    "action_dispatch_id": action_dispatch_id,
                    "idempotency_key": idempotency_key,
                    "status": "queued",
                }
            ]
        }

    def finalize(state: RouteRiskState) -> dict[str, object]:
        approval = state.get("approval")
        if isinstance(approval, dict):
            return {"final_status": str(approval.get("decision", "rejected"))}
        return {"final_status": "assessed"}

    builder = state_graph(RouteRiskState)
    builder.add_node("load_memory_context", load_memory_context)
    builder.add_node("react_assistant", react_assistant)
    builder.add_node("tools", run_read_only_tool)
    builder.add_node("recommendation", validate_recommendation)
    builder.add_node("verify_alternative_eligibility", verify_alternative_eligibility)
    builder.add_node("rank_eligible_recovery_options", rank_eligible_recovery_options)
    builder.add_node("attach_recovery_ranking", attach_recovery_ranking)
    builder.add_node("policy_gate", policy_gate)
    builder.add_node("approval_gate", approval_gate)
    builder.add_node("queue_approved_action", queue_approved_action)
    builder.add_node("finalize", finalize)
    builder.add_conditional_edges(start, route_by_severity)
    builder.add_edge("load_memory_context", "react_assistant")
    builder.add_conditional_edges("react_assistant", route_after_assistant)
    builder.add_edge("tools", "react_assistant")
    builder.add_edge("recommendation", "verify_alternative_eligibility")
    builder.add_edge("verify_alternative_eligibility", "rank_eligible_recovery_options")
    builder.add_edge("rank_eligible_recovery_options", "attach_recovery_ranking")
    builder.add_edge("attach_recovery_ranking", "policy_gate")
    builder.add_edge("policy_gate", "approval_gate")
    builder.add_edge("approval_gate", "queue_approved_action")
    builder.add_edge("queue_approved_action", "finalize")
    builder.add_edge("finalize", end)
    return builder.compile(checkpointer=checkpointer or in_memory_saver())


def compile_with_postgres(factory: Callable[[], Any]) -> Any:
    """Compile with the application's production checkpointer factory.

    Keeping the factory injected prevents local code from constructing a database
    client or reading credentials at import time.
    """
    return build_graph(checkpointer=factory())
