from agent.graph import MAX_REACT_ITERATIONS, _next_tool, _recommendation, route_by_severity
from agent.state import thread_id_for


def graph_state(*, severity="high", unavailable=None, iterations=0):
    return {
        "tenant_id": "acme",
        "trip_id": "trip-1",
        "incident_id": "incident-1",
        "risk_assessment": {"severity": severity},
        "evidence": [
            {"evidence_id": "evidence-1", "source_type": "flight_status"},
            {"evidence_id": "evidence-2", "source_type": "connection_feasibility"},
        ],
        "source_health": {"core_sources_unavailable": unavailable or []},
        "react_iterations": iterations,
        "tool_audit": [],
    }


def test_thread_id_is_tenant_scoped_and_stable():
    assert (
        thread_id_for(tenant_id="acme", trip_id="trip-1", incident_id="incident-1")
        == "tenant:acme:trip:trip-1:incident:incident-1"
    )


def test_high_risk_routes_to_react_but_core_outages_skip_it():
    assert route_by_severity(graph_state()) == "load_memory_context"
    assert route_by_severity(graph_state(unavailable=["flight_status"])) == "approval_gate"
    assert (
        route_by_severity(graph_state(unavailable=["flight_status", "airport_weather"]))
        == "approval_gate"
    )


def test_tool_selection_is_bounded_and_recommendations_cite_known_evidence():
    state = graph_state()
    assert _next_tool(state) == "get_flight_status"
    state["react_iterations"] = MAX_REACT_ITERATIONS
    assert _next_tool(state) is None
    assert _recommendation(state)["evidence_ids"] == ["evidence-1", "evidence-2"]
