import pytest

import tools.openai_provider as openai_provider
from tools.openai_provider import OpenAIRecommendationProvider


def test_openai_provider_is_fail_closed_without_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENABLED", "true")
    provider = OpenAIRecommendationProvider()
    assert provider.available is False
    with pytest.raises(RuntimeError, match="disabled"):
        provider.recommend(
            incident_id="00000000-0000-0000-0000-000000000001", risk_assessment={}, evidence=[]
        )


def test_runtime_control_can_enable_a_keyed_provider_without_process_restart(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_ENABLED", "false")
    provider = OpenAIRecommendationProvider()
    assert provider.available is False
    assert provider.available_for(enabled=True) is True
    assert provider.audit_metadata(
        invocation_type="tool_selection", outcome="tool_selected"
    ) == {
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "prompt_version": "tool-selection-2026-08-09",
        "invocation_type": "tool_selection",
        "outcome": "tool_selected",
        "token_usage": None,
        "estimated_cost_usd": None,
    }


def test_tool_selection_accepts_one_validated_read_only_request(monkeypatch):
    captured: dict[str, object] = {}

    class FakeMessage:
        tool_calls = [{"name": "get_flight_status", "args": {"trip_id": "trip-1"}}]

    class FakeModel:
        def bind_tools(self, tools, **kwargs):
            captured["tools"] = tools
            captured["bind_kwargs"] = kwargs
            return self

        def invoke(self, messages):
            captured["messages"] = messages
            return FakeMessage()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(openai_provider, "ChatOpenAI", lambda **_: FakeModel())
    selected = OpenAIRecommendationProvider().select_tool(
        trip_id="trip-1",
        risk_assessment={"severity": "high"},
        evidence=[],
        source_health={"core_sources_unavailable": []},
        enabled=True,
    )
    assert selected is not None
    assert selected.name == "get_flight_status"
    assert selected.arguments == {"trip_id": "trip-1"}
    assert captured["bind_kwargs"] == {"tool_choice": "auto", "parallel_tool_calls": False}


def test_tool_selection_rejects_extra_or_cross_trip_arguments(monkeypatch):
    class FakeMessage:
        tool_calls = [
            {
                "name": "get_flight_status",
                "args": {"trip_id": "another-trip", "extra": "not allowed"},
            }
        ]

    class FakeModel:
        def bind_tools(self, *_args, **_kwargs):
            return self

        def invoke(self, _messages):
            return FakeMessage()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(openai_provider, "ChatOpenAI", lambda **_: FakeModel())
    selected = OpenAIRecommendationProvider().select_tool(
        trip_id="trip-1",
        risk_assessment={"severity": "high"},
        evidence=[],
        source_health={"core_sources_unavailable": []},
        enabled=True,
    )
    assert selected is None
