"""The single server-side OpenAI/LangChain integration point for RouteShield."""

from __future__ import annotations

import json
import os
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from domain.models import Recommendation

SYSTEM_PROMPT = """You are RouteShield's disruption-investigation assistant.
Use only the supplied evidence. Cite only supplied evidence IDs. Never change the
deterministic risk score or severity. Never invent provider facts, alternatives,
policy decisions, bookings, payments, cancellations, or refunds. Return high
uncertainty when evidence is stale or incomplete."""
RECOMMENDATION_PROMPT_VERSION = "recommendation-2026-08-09"

TOOL_SELECTION_SYSTEM_PROMPT = """You are RouteShield's bounded disruption-investigation planner.
You may request at most one supplied read-only tool when it would materially improve a
manager's review. Use only the trip ID supplied in the request. Do not request a tool
when the supplied evidence is sufficient. Never request a booking, notification,
payment, cancellation, refund, policy exception, or profile write."""
TOOL_SELECTION_PROMPT_VERSION = "tool-selection-2026-08-09"

# These schemas are sent only to the server-side model integration.  The graph validates
# the returned name and arguments again before it records or executes anything.
READ_ONLY_TOOL_SCHEMAS: tuple[dict[str, object], ...] = (
    {
        "type": "function",
        "function": {
            "name": "get_trip_context",
            "description": "Retrieve the already scoped itinerary context for a trip.",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "string"}},
                "required": ["trip_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_flight_status",
            "description": "Retrieve the normalized current flight status for a trip.",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "string"}},
                "required": ["trip_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_connection_feasibility",
            "description": "Recalculate connection feasibility for a trip.",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "string"}},
                "required": ["trip_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_airport_weather",
            "description": "Retrieve normalized airport weather risk for a trip.",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "string"}},
                "required": ["trip_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ground_route_risk",
            "description": "Retrieve normalized ground-route risk for a trip.",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "string"}},
                "required": ["trip_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_destination_advisory",
            "description": "Retrieve normalized destination advisory context for a trip.",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "string"}},
                "required": ["trip_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_alternative_flights",
            "description": "Retrieve available alternative flights for a trip.",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "string"}},
                "required": ["trip_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_corporate_travel_policy",
            "description": "Retrieve the applicable corporate travel policy for a trip.",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "string"}},
                "required": ["trip_id"],
                "additionalProperties": False,
            },
        },
    },
)
READ_ONLY_TOOL_NAMES = tuple(
    str(schema["function"]["name"])
    for schema in READ_ONLY_TOOL_SCHEMAS
    if isinstance(schema.get("function"), dict)
)


class RecommendationOutput(BaseModel):
    severity_explanation: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[UUID] = Field(min_length=1)
    uncertainty: str
    recommended_action: str = Field(min_length=1, max_length=1000)
    ranked_alternative_ids: list[str] = Field(default_factory=list)
    traveler_message: str = Field(min_length=1, max_length=1000)
    manager_message: str = Field(min_length=1, max_length=2000)
    requires_human_approval: bool = True
    missing_information: list[str] = Field(default_factory=list)


class ToolSelection(BaseModel):
    """The one safe tool request the model may make for an assessment."""

    name: str
    arguments: dict[str, object]


class OpenAIRecommendationProvider:
    def __init__(self) -> None:
        self.model = os.getenv("OPENAI_MODEL_PRIMARY", "gpt-5.6-terra")
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.enabled = os.getenv("LLM_ENABLED", "false").lower() == "true"

    @property
    def available(self) -> bool:
        return self.available_for(enabled=self.enabled)

    def available_for(self, *, enabled: bool) -> bool:
        """Check a per-assessment runtime control without exposing the API key."""
        return enabled and bool(self.api_key)

    def audit_metadata(self, *, invocation_type: str, outcome: str) -> dict[str, object]:
        """Return safe metadata suitable for durable incident audit records.

        LangChain's structured-output wrapper does not reliably expose provider usage
        across every supported model, so token and cost values are explicitly absent
        rather than estimated or fabricated.  A future provider adapter can populate
        these fields without changing the incident schema.
        """
        prompt_version = (
            TOOL_SELECTION_PROMPT_VERSION
            if invocation_type == "tool_selection"
            else RECOMMENDATION_PROMPT_VERSION
        )
        return {
            "provider": "openai",
            "model": self.model,
            "prompt_version": prompt_version,
            "invocation_type": invocation_type,
            "outcome": outcome,
            "token_usage": None,
            "estimated_cost_usd": None,
        }

    def select_tool(
        self,
        *,
        trip_id: str,
        risk_assessment: dict[str, object],
        evidence: list[dict[str, object]],
        source_health: dict[str, object],
        enabled: bool | None = None,
    ) -> ToolSelection | None:
        """Ask the model for one allow-listed, read-only follow-up lookup.

        Returning ``None`` is a valid decision: it means the available, validated
        evidence is sufficient for the deterministic recommendation path.  The
        caller still validates any returned name and arguments before executing it.
        """
        if not self.available_for(enabled=self.enabled if enabled is None else enabled):
            raise RuntimeError("OpenAI tool selection is disabled or OPENAI_API_KEY is unavailable")
        model = ChatOpenAI(model=self.model, api_key=self.api_key, temperature=0)
        tool_model = model.bind_tools(
            list(READ_ONLY_TOOL_SCHEMAS), tool_choice="auto", parallel_tool_calls=False
        )
        response = tool_model.invoke(
            [
                SystemMessage(TOOL_SELECTION_SYSTEM_PROMPT),
                HumanMessage(
                    content=json.dumps(
                        {
                            "trip_id": trip_id,
                            "risk_assessment": risk_assessment,
                            "evidence": evidence,
                            "source_health": source_health,
                            "instruction": "Request at most one follow-up tool, or none.",
                        },
                        default=str,
                    )
                ),
            ]
        )
        tool_calls = getattr(response, "tool_calls", [])
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            return None
        call = tool_calls[0]
        if not isinstance(call, dict):
            return None
        name = call.get("name")
        arguments = call.get("args")
        if name not in READ_ONLY_TOOL_NAMES or not isinstance(arguments, dict):
            return None
        if arguments.get("trip_id") != trip_id or set(arguments) != {"trip_id"}:
            return None
        return ToolSelection(name=name, arguments=arguments)

    def recommend(
        self,
        *,
        incident_id: UUID,
        risk_assessment: dict[str, object],
        evidence: list[dict[str, object]],
        enabled: bool | None = None,
    ) -> Recommendation:
        if not self.available_for(enabled=self.enabled if enabled is None else enabled):
            raise RuntimeError(
                "OpenAI recommendations are disabled or OPENAI_API_KEY is unavailable"
            )
        model = ChatOpenAI(model=self.model, api_key=self.api_key, temperature=0)
        structured_model = model.with_structured_output(RecommendationOutput)
        output = structured_model.invoke(
            [
                SystemMessage(SYSTEM_PROMPT),
                HumanMessage(
                    content=json.dumps(
                        {
                            "risk_assessment": risk_assessment,
                            "evidence": evidence,
                            "instruction": "Produce a manager-review recommendation only.",
                        },
                        default=str,
                    )
                ),
            ]
        )
        return Recommendation(incident_id=incident_id, **output.model_dump())
