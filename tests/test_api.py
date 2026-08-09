import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient

import apps.api.main as api_main
from apps.api.main import app, repository

client = TestClient(app)
HEADERS = {"X-Tenant-Id": "acme", "X-Actor-Role": "travel_manager"}


def trip_payload():
    return {
        "tenant_id": "acme",
        "traveler_id": "traveler-1",
        "trip_criticality": "business_critical",
        "ground_origin": "1 Market St, San Francisco, CA",
        "destination_country": "US",
        "segments": [
            {
                "segment_id": "sfo-den-1",
                "carrier_code": "UA",
                "flight_number": "123",
                "departure_airport": "SFO",
                "arrival_airport": "DEN",
                "scheduled_departure_at": "2026-08-01T15:00:00Z",
                "scheduled_arrival_at": "2026-08-01T18:30:00Z",
            }
        ],
    }


def setup_function():
    repository.trips.clear()
    repository.trip_segments.clear()
    repository.evidence.clear()
    repository.assessments.clear()
    repository.incidents.clear()
    repository.approvals.clear()
    repository.events.clear()
    repository.profiles.clear()
    repository.memory_proposals.clear()
    repository.idempotency_records.clear()
    repository.candidate_sets.clear()
    repository.candidate_outcomes.clear()
    repository.manager_feedback.clear()
    repository.tenant_playbooks.clear()
    repository.action_dispatches.clear()
    repository.action_dispatch_attempts.clear()
    repository.runtime_control_overrides.clear()
    repository.runtime_control_changes.clear()
    repository.platform_control_overrides.clear()
    repository.platform_control_changes.clear()
    repository.original_uploads.clear()


def test_trip_requires_matching_tenant():
    response = client.post("/v1/trips", json=trip_payload(), headers={"X-Tenant-Id": "other"})
    assert response.status_code == 403


def test_assessment_marks_missing_sources_as_unknown():
    created = client.post("/v1/trips", json=trip_payload(), headers=HEADERS)
    trip_id = created.json()["trip_id"]
    response = client.post(f"/v1/trips/{trip_id}/assess", headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["assessment"]["severity"] == "low"
    assert result["assessment"]["uncertainty"] == "low"
    assert result["disposition"] == "monitor"


def test_trip_segments_are_normalized_for_operational_queries():
    trip = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()
    segments = repository.trip_segments[UUID(trip["trip_id"])]
    assert len(segments) == 1
    assert segments[0].departure_airport == "SFO"


def test_csv_import_quarantines_original_content_and_records_only_metadata():
    csv_content = (
        "tenant_id,traveler_id,segment_id,carrier_code,flight_number\n"
        "acme,traveler-1,sfo-den-1,UA,123\n"
    )
    response = client.post("/v1/trips/import/csv", content=csv_content, headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["validated_rows"] == 1
    upload = repository.original_uploads[UUID(result["original_upload_id"])]
    assert upload.status.value == "validated"
    assert csv_content not in upload.model_dump_json()
    assert upload.object_key.startswith("quarantine/acme/")


def test_csv_import_rejects_formula_content_after_quarantine():
    csv_content = (
        "tenant_id,traveler_id,segment_id,carrier_code,flight_number\n"
        "acme,=formula,sfo-den-1,UA,123\n"
    )
    response = client.post("/v1/trips/import/csv", content=csv_content, headers=HEADERS)
    assert response.status_code == 422
    upload = next(iter(repository.original_uploads.values()))
    assert upload.status.value == "rejected"
    assert "formula" in upload.validation_errors[0]


def test_assessment_uses_fresh_evidence():
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    evidence = [
        {
            "source_name": "fixture",
            "source_type": kind,
            "source_url_or_record_id": kind,
            "normalized_payload": {"risk_score": score},
        }
        for kind, score in [
            ("flight_status", 100),
            ("connection_feasibility", 50),
            ("airport_weather", 80),
            ("ground_route", 0),
            ("destination_advisory", 0),
        ]
    ]
    upload = client.put(f"/v1/trips/{trip_id}/evidence", json=evidence, headers=HEADERS)
    assert upload.status_code == 204
    result = client.post(f"/v1/trips/{trip_id}/assess", headers=HEADERS).json()
    assert result["assessment"]["risk_score"] == 66
    assert result["assessment"]["severity"] == "high"


def test_disruption_fixture_routes_to_bounded_investigation():
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    result = client.post(f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=HEADERS).json()
    assert result["assessment"]["severity"] == "high"
    assert result["disposition"] == "investigate"
    assert result["source_health"]["limited_visibility"] is False


def test_two_core_source_outages_require_human_review():
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    result = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=source_outage", headers=HEADERS
    ).json()
    assert result["disposition"] == "needs_human_review"
    assert set(result["source_health"]["core_sources_unavailable"]) == {
        "flight_status",
        "airport_weather",
    }
    incident = client.get(f"/v1/incidents/{result['incident_id']}", headers=HEADERS).json()
    assert incident["tool_audit"] == []
    assert incident["recommendation"]["uncertainty"] == "high"


def test_high_risk_with_unavailable_flight_status_requires_human_review():
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    evidence = [
        {
            "source_name": "fixture",
            "source_type": source_type,
            "source_url_or_record_id": source_type,
            "freshness_status": freshness_status,
            "normalized_payload": payload,
        }
        for source_type, freshness_status, payload in [
            ("flight_status", "unavailable", {}),
            ("connection_feasibility", "fresh", {"risk_score": 100}),
            ("airport_weather", "fresh", {"risk_score": 100}),
            ("ground_route", "fresh", {"risk_score": 100}),
            ("destination_advisory", "fresh", {"risk_score": 100}),
        ]
    ]
    upload = client.put(f"/v1/trips/{trip_id}/evidence", json=evidence, headers=HEADERS)
    assert upload.status_code == 204
    result = client.post(f"/v1/trips/{trip_id}/assess", headers=HEADERS).json()
    assert result["assessment"]["severity"] == "high"
    assert result["disposition"] == "needs_human_review"


def test_high_risk_incident_has_bounded_read_only_audit_and_requires_approval():
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    result = client.post(f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=HEADERS).json()
    incident_id = result["incident_id"]
    incident = client.get(f"/v1/incidents/{incident_id}", headers=HEADERS).json()
    assert incident["status"] == "pending_approval"
    assert len(incident["tool_audit"]) == 5
    assert incident["recommendation"]["requires_human_approval"] is True
    assert incident["recommendation"]["ranked_alternative_ids"] == [
        "fixture-alt-1",
        "fixture-alt-2",
    ]
    assert incident["approval_payload"]["policy_gate"]["status"] == "human_approval_required"

    approval = client.post(
        f"/v1/incidents/{incident_id}/approve",
        headers={**HEADERS, "Idempotency-Key": "approval-1"},
        json={"decision": "approve", "actor_id": "manager-1", "reason": "reviewed"},
    )
    assert approval.status_code == 200
    assert (
        client.get(f"/v1/incidents/{incident_id}", headers=HEADERS).json()["status"] == "approved"
    )


def test_assigned_manager_can_view_only_the_selected_incident_audit_trail():
    manager_headers = {**HEADERS, "X-Actor-Id": "manager-1"}
    trip = client.post("/v1/trips", json=trip_payload(), headers=manager_headers).json()
    trip_id = trip["trip_id"]
    assessment = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=manager_headers
    ).json()
    incident_response = client.get(
        f"/v1/incidents/{assessment['incident_id']}", headers=manager_headers
    )
    incident = incident_response.json()

    timeline = client.get(
        f"/v1/runs/{incident['correlation_id']}/events", headers=manager_headers
    )

    assert timeline.status_code == 200
    assert timeline.json()
    assert {event["correlation_id"] for event in timeline.json()} == {incident["correlation_id"]}


def test_candidate_sets_are_server_scored_and_outcomes_are_append_only():
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    incident_id = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=HEADERS
    ).json()["incident_id"]
    candidates = {
        "candidates": [
            {
                "candidate_id": "top",
                "available": True,
                "policy_compliant": True,
                "accessibility_compliant": True,
                "feasible": True,
                "arrival_delay_minutes": 20,
                "incremental_cost": 50,
                "connection_minutes": 90,
            },
            {
                "candidate_id": "second",
                "available": True,
                "policy_compliant": True,
                "accessibility_compliant": True,
                "feasible": True,
                "arrival_delay_minutes": 30,
                "incremental_cost": 60,
                "connection_minutes": 90,
            },
            {
                "candidate_id": "over-budget",
                "available": True,
                "policy_compliant": True,
                "accessibility_compliant": True,
                "feasible": True,
                "arrival_delay_minutes": 0,
                "incremental_cost": 750,
                "connection_minutes": 90,
            },
            {
                "candidate_id": "unavailable",
                "available": False,
                "policy_compliant": True,
                "accessibility_compliant": True,
                "feasible": True,
                "arrival_delay_minutes": 0,
                "incremental_cost": 0,
                "connection_minutes": 90,
            },
        ]
    }
    created = client.post(
        f"/v1/incidents/{incident_id}/candidate-sets",
        headers={**HEADERS, "Idempotency-Key": "candidate-set-1"},
        json=candidates,
    )
    assert created.status_code == 200
    candidate_set = created.json()
    candidate_by_id = {item["candidate_id"]: item for item in candidate_set["candidates"]}
    assert candidate_by_id["top"]["displayed_position"] == 1
    assert candidate_by_id["second"]["displayed_position"] == 2
    assert candidate_by_id["over-budget"]["lifecycle_state"] == "policy_ineligible"
    assert candidate_by_id["over-budget"]["deterministic_recovery_score"] is None
    assert candidate_by_id["unavailable"]["lifecycle_state"] == "unavailable"

    outcome_url = (
        f"/v1/incidents/{incident_id}/candidate-sets/{candidate_set['candidate_set_id']}/"
        "candidates/second/outcomes"
    )
    offered = client.post(outcome_url, headers=HEADERS, json={"state": "offered"})
    assert offered.status_code == 200
    viewed = client.post(
        outcome_url,
        headers={
            "X-Tenant-Id": "acme",
            "X-Actor-Role": "traveler",
            "X-Actor-Id": "traveler-1",
        },
        json={"state": "viewed"},
    )
    assert viewed.status_code == 200
    selected = client.post(
        outcome_url,
        headers=HEADERS,
        json={"state": "selected", "manager_override_reason": "shorter ground transfer"},
    )
    assert selected.status_code == 200
    completed = client.post(
        outcome_url,
        headers=HEADERS,
        json={
            "state": "completed",
            "final_itinerary": {"confirmation_reference": "safe-ref"},
            "material_outcome": {"arrival_delay_minutes": 25},
        },
    )
    assert completed.status_code == 200
    result = completed.json()
    assert result["selected_candidate_id"] == "second"
    assert next(
        item for item in result["candidates"] if item["candidate_id"] == "second"
    )["lifecycle_state"] == "completed"
    assert [item["state"] for item in result["outcomes"]] == [
        "offered",
        "viewed",
        "selected",
        "completed",
    ]
    # The ranked set remains an immutable snapshot; lifecycle observations are separate records.
    stored = repository.candidate_sets[UUID(candidate_set["candidate_set_id"])][1]
    assert stored.outcomes == []

    feedback = client.get(
        f"/v1/incidents/{incident_id}/manager-feedback", headers=HEADERS
    )
    assert feedback.status_code == 200
    assert {item["feedback_type"] for item in feedback.json()} == {
        "manager_override",
        "recovery_outcome",
    }

    invalid = client.post(
        (
            f"/v1/incidents/{incident_id}/candidate-sets/{candidate_set['candidate_set_id']}/"
            "candidates/unavailable/outcomes"
        ),
        headers=HEADERS,
        json={"state": "offered"},
    )
    assert invalid.status_code == 409


def test_approval_idempotency_replays_the_same_record_and_rejects_payload_changes():
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    incident_id = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=HEADERS
    ).json()["incident_id"]
    headers = {**HEADERS, "Idempotency-Key": "stable-approval"}
    payload = {"decision": "approve", "actor_id": "manager-1", "reason": "reviewed"}
    first = client.post(f"/v1/incidents/{incident_id}/approve", headers=headers, json=payload)
    replay = client.post(f"/v1/incidents/{incident_id}/approve", headers=headers, json=payload)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()

    changed = client.post(
        f"/v1/incidents/{incident_id}/approve",
        headers=headers,
        json={**payload, "reason": "different reason"},
    )
    assert changed.status_code == 409


def test_approved_action_is_placed_in_the_durable_outbox_before_dispatch(monkeypatch):
    monkeypatch.setattr(
        api_main, "controls", replace(api_main.controls, approval_actions_enabled=True)
    )
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    incident_id = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=HEADERS
    ).json()["incident_id"]
    approval = client.post(
        f"/v1/incidents/{incident_id}/approve",
        headers={**HEADERS, "Idempotency-Key": "approved-action-outbox"},
        json={
            "decision": "approve",
            "actor_id": "manager-1",
            "reason": "reviewed",
            "final_action_payload": {"kind": "booking_action_request"},
        },
    )
    assert approval.status_code == 200
    assert approval.json()["action_dispatch_status"] == "pending"
    actions = client.get(f"/v1/incidents/{incident_id}/actions", headers=HEADERS)
    assert actions.status_code == 200
    assert actions.json()[0]["status"] == "queued"
    assert actions.json()[0]["action_payload"] == {"kind": "booking_action_request"}


def test_memory_proposal_requires_confirmation_and_stays_tenant_scoped():
    proposal = {
        "tenant_id": "acme",
        "traveler_id": "traveler-1",
        "record_id": "traveler-1",
        "patch": {
            "preferred_airports": ["SFO"],
            "minimum_connection_minutes": 80,
            "consent_version": "v1",
        },
        "source_message_id": "message-1",
        "confidence": 0.9,
        "actor_id": "agent",
    }
    created = client.post("/v1/memory/proposals", headers=HEADERS, json=proposal)
    assert created.status_code == 201
    proposal_id = created.json()["proposal_id"]
    assert client.get("/v1/travelers/traveler-1/preferences", headers=HEADERS).json() is None

    confirmed = client.post(
        f"/v1/memory/proposals/{proposal_id}/confirm?actor_id=traveler-1",
        headers={
            "X-Tenant-Id": "acme",
            "X-Actor-Role": "traveler",
            "X-Actor-Id": "traveler-1",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["preferred_airports"] == ["SFO"]
    assert confirmed.json()["minimum_connection_minutes"] == 80
    assert repository.memory_proposals[UUID(proposal_id)].status.value == "confirmed"
    assert (
        client.get(
            "/v1/travelers/traveler-1/preferences",
            headers={"X-Tenant-Id": "other", "X-Actor-Role": "travel_manager"},
        ).json()
        is None
    )

    # A new incident receives only this traveler's confirmed, tenant-scoped profile.
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    incident_id = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=HEADERS
    ).json()["incident_id"]
    candidate_set = next(
        item
        for _, item in repository.candidate_sets.values()
        if str(item.incident_id) == incident_id
    )
    candidate_by_id = {item.candidate_id: item for item in candidate_set.candidates}
    assert candidate_by_id["fixture-alt-1"].eligible is False
    assert "traveler_minimum_connection_not_met" in candidate_by_id[
        "fixture-alt-1"
    ].exclusion_reasons


def test_traveler_can_make_an_explicit_audited_notification_preference_change():
    headers = {
        "X-Tenant-Id": "acme",
        "X-Actor-Role": "traveler",
        "X-Actor-Id": "traveler-1",
        "Idempotency-Key": "explicit-preference-1",
    }
    changed = client.put(
        "/v1/travelers/traveler-1/preferences",
        headers=headers,
        json={
            "notification_channel": "in_app",
            "language": "en",
            "consent_version": "settings-v1",
        },
    )
    assert changed.status_code == 200
    assert changed.json()["notification_channel"] == "in_app"
    assert changed.json()["version"] == 1
    audit = repository.memory_audits[-1]
    assert audit.action == "updated"
    assert audit.details["source"] == "explicit_settings_api"
    denied = client.put(
        "/v1/travelers/traveler-1/preferences",
        headers={**headers, "X-Actor-Id": "traveler-2", "Idempotency-Key": "other"},
        json={"notification_channel": "in_app", "consent_version": "settings-v1"},
    )
    assert denied.status_code == 403


def test_traveler_access_is_scoped_to_their_own_trip_and_incidents_stay_manager_only():
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    traveler_headers = {
        "X-Tenant-Id": "acme",
        "X-Actor-Role": "traveler",
        "X-Actor-Id": "traveler-2",
    }
    assert client.get(f"/v1/trips/{trip_id}", headers=traveler_headers).status_code == 403

    own_headers = {**traveler_headers, "X-Actor-Id": "traveler-1"}
    assert client.get(f"/v1/trips/{trip_id}", headers=own_headers).status_code == 200

    incident_id = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=HEADERS
    ).json()["incident_id"]
    assert client.get(f"/v1/incidents/{incident_id}", headers=own_headers).status_code == 403


def test_manager_access_requires_a_trip_assignment_when_an_actor_is_known():
    manager_one = {**HEADERS, "X-Actor-Id": "manager-1"}
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=manager_one).json()["trip_id"]
    assert client.get(f"/v1/trips/{trip_id}", headers=manager_one).status_code == 200

    manager_two = {**HEADERS, "X-Actor-Id": "manager-2"}
    assert client.get(f"/v1/trips/{trip_id}", headers=manager_two).status_code == 403

    reassigned = client.put(
        f"/v1/trips/{trip_id}/assignment",
        json={"manager_id": "manager-2"},
        headers={"X-Tenant-Id": "acme", "X-Actor-Role": "tenant_admin"},
    )
    assert reassigned.status_code == 200
    assert client.get(f"/v1/trips/{trip_id}", headers=manager_two).status_code == 200


def test_traveler_incident_view_excludes_manager_rationale_until_guidance_is_approved():
    manager_headers = {**HEADERS, "X-Actor-Id": "manager-1"}
    trip_id = client.post(
        "/v1/trips", json=trip_payload(), headers=manager_headers
    ).json()["trip_id"]
    incident_id = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=manager_headers
    ).json()["incident_id"]
    assert client.post(
        f"/v1/incidents/{incident_id}/approve",
        headers={**manager_headers, "Idempotency-Key": "traveler-guidance"},
        json={"decision": "approve", "actor_id": "manager-1", "reason": "reviewed"},
    ).status_code == 200

    response = client.get(
        "/v1/travelers/traveler-1/incidents",
        headers={
            "X-Tenant-Id": "acme",
            "X-Actor-Role": "traveler",
            "X-Actor-Id": "traveler-1",
        },
    )
    assert response.status_code == 200
    view = response.json()[0]
    assert view["incident_id"] == incident_id
    assert view["approved_guidance"]
    assert "manager_message" not in view


def test_signed_in_actor_cannot_spoof_an_approval_actor_id():
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    incident_id = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=HEADERS
    ).json()["incident_id"]
    response = client.post(
        f"/v1/incidents/{incident_id}/approve",
        headers={**HEADERS, "X-Actor-Id": "manager-1", "Idempotency-Key": "actor-binding"},
        json={"decision": "approve", "actor_id": "manager-2", "reason": "reviewed"},
    )
    assert response.status_code == 403


def test_disabled_approval_actions_preserve_the_manager_decision_but_suppress_dispatch(monkeypatch):
    monkeypatch.setattr(
        api_main, "controls", replace(api_main.controls, approval_actions_enabled=False)
    )
    manager_headers = {**HEADERS, "X-Actor-Id": "manager-1"}
    trip_id = client.post(
        "/v1/trips", json=trip_payload(), headers=manager_headers
    ).json()["trip_id"]
    incident_id = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=manager_headers
    ).json()["incident_id"]
    response = client.post(
        f"/v1/incidents/{incident_id}/approve",
        headers={**manager_headers, "Idempotency-Key": "suppressed-action"},
        json={
            "decision": "approve",
            "actor_id": "manager-1",
            "reason": "reviewed",
            "final_action_payload": {"kind": "booking_action_request"},
        },
    )
    assert response.status_code == 200
    assert response.json()["action_dispatch_status"] == "suppressed"
    assert client.get(f"/v1/incidents/{incident_id}", headers=manager_headers).json()[
        "status"
    ] == "approved"


def test_tenant_runtime_control_change_is_audited_and_applies_without_redeploy():
    admin_headers = {
        "X-Tenant-Id": "acme",
        "X-Actor-Role": "tenant_admin",
        "X-Actor-Id": "admin-1",
    }
    changed = client.put(
        "/v1/governance/runtime-controls/REACT_TOOL_CALLS_ENABLED",
        headers=admin_headers,
        json={"enabled": False, "reason": "provider investigation maintenance"},
    )
    assert changed.status_code == 200
    record = changed.json()
    assert record["previous_enabled"] is True
    assert record["enabled"] is False
    assert record["actor_id"] == "admin-1"

    manager_headers = {**HEADERS, "X-Actor-Id": "manager-1"}
    trip_id = client.post(
        "/v1/trips", json=trip_payload(), headers=manager_headers
    ).json()["trip_id"]
    incident_id = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=manager_headers
    ).json()["incident_id"]
    incident = client.get(f"/v1/incidents/{incident_id}", headers=manager_headers).json()
    assert incident["tool_audit"] == []
    assert client.get("/v1/governance/runtime-controls", headers=admin_headers).json()[0][
        "control_name"
    ] == "REACT_TOOL_CALLS_ENABLED"


def test_platform_admin_controls_global_defaults_without_tenant_data_access():
    platform_headers = {
        "X-Actor-Role": "platform_admin",
        "X-Actor-Id": "platform-1",
        "Idempotency-Key": "platform-control-1",
    }
    changed = client.put(
        "/v1/platform/runtime-controls/REACT_TOOL_CALLS_ENABLED",
        headers=platform_headers,
        json={"enabled": False, "reason": "platform maintenance"},
    )
    assert changed.status_code == 200
    assert changed.json()["scope"] == "platform"
    assert changed.json()["actor_id"] == "platform-1"
    controls = client.get("/v1/platform/runtime-controls", headers=platform_headers)
    assert controls.status_code == 200
    assert controls.json()[0]["control_name"] == "REACT_TOOL_CALLS_ENABLED"

    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    incident_id = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=HEADERS
    ).json()["incident_id"]
    incident = client.get(f"/v1/incidents/{incident_id}", headers=HEADERS).json()
    assert incident["tool_audit"] == []
    # Platform operators cannot use a tenant-data endpoint, even when they supply a tenant header.
    assert (
        client.get(
            f"/v1/trips/{trip_id}",
            headers={**platform_headers, "X-Tenant-Id": "acme"},
        ).status_code
        == 403
    )


def test_admin_playbooks_are_immutable_and_available_only_as_read_only_graph_guidance():
    admin_headers = {
        "X-Tenant-Id": "acme",
        "X-Actor-Role": "tenant_admin",
        "X-Actor-Id": "admin-1",
        "Idempotency-Key": "playbook-incident-v1",
    }
    payload = {
        "name": "incident-escalation",
        "version": "v1",
        "guidance": "Escalate Critical airport closures to the duty manager within 15 minutes.",
    }
    created = client.post("/v1/governance/playbooks", headers=admin_headers, json=payload)
    assert created.status_code == 201
    assert created.json()["approved_by"] == "admin-1"

    duplicate = client.post("/v1/governance/playbooks", headers=admin_headers, json=payload)
    assert duplicate.status_code == 201
    assert duplicate.json()["playbook_id"] == created.json()["playbook_id"]
    changed = client.post(
        "/v1/governance/playbooks",
        headers={**admin_headers, "Idempotency-Key": "playbook-incident-v1-changed"},
        json={**payload, "guidance": "Different guidance"},
    )
    assert changed.status_code == 409

    manager_playbooks = client.get("/v1/governance/playbooks", headers=HEADERS)
    assert manager_playbooks.status_code == 200
    assert manager_playbooks.json()[0]["guidance"] == payload["guidance"]
    traveler_playbooks = client.get(
        "/v1/governance/playbooks",
        headers={"X-Tenant-Id": "acme", "X-Actor-Role": "traveler", "X-Actor-Id": "traveler-1"},
    )
    assert traveler_playbooks.status_code == 403

    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    incident_id = client.post(
        f"/v1/trips/{trip_id}/assess?scenario=disruption", headers=HEADERS
    ).json()["incident_id"]
    incident = client.get(f"/v1/incidents/{incident_id}", headers=HEADERS).json()
    assert incident["approval_payload"]["approved_playbooks"] == [
        {
            "playbook_id": created.json()["playbook_id"],
            "name": "incident-escalation",
            "version": "v1",
            "guidance": payload["guidance"],
        }
    ]
    # Guidance is supplied to the approval workflow, never appended to the model's prompt.
    assert payload["guidance"] not in incident["recommendation"]["manager_message"]


def test_deployed_evidence_mode_uses_live_adapters_and_reports_disabled_sources(monkeypatch):
    monkeypatch.setattr(
        api_main, "controls", replace(api_main.controls, demo_evidence_enabled=False)
    )
    trip_id = client.post("/v1/trips", json=trip_payload(), headers=HEADERS).json()["trip_id"]
    assessment = client.post(f"/v1/trips/{trip_id}/assess", headers=HEADERS).json()
    assert assessment["disposition"] == "needs_human_review"
    assert {item["error_code"] for item in assessment["evidence"] if item["error_code"]} == {
        "provider_disabled"
    }
    assert all(not item["source_name"].startswith("fixture-") for item in assessment["evidence"])


def test_due_assessment_job_claims_each_window_once():
    payload = trip_payload()
    departure = datetime.now(UTC) + timedelta(hours=24)
    payload["segments"][0]["scheduled_departure_at"] = departure.isoformat()
    payload["segments"][0]["scheduled_arrival_at"] = (departure + timedelta(hours=3)).isoformat()
    trip_id = client.post("/v1/trips", json=payload, headers=HEADERS).json()["trip_id"]

    first = asyncio.run(api_main.process_due_assessments(now=datetime.now(UTC)))
    replay = asyncio.run(api_main.process_due_assessments(now=datetime.now(UTC)))
    assert first == {"assessed": 1, "replayed": 0, "skipped": 0}
    assert replay == {"assessed": 0, "replayed": 1, "skipped": 0}
    assert len(repository.assessments[UUID(trip_id)]) == 1


def test_production_mode_rejects_header_authentication_without_a_bearer_token(monkeypatch):
    monkeypatch.setattr(api_main, "controls", replace(api_main.controls, require_oidc=True))
    response = client.get("/v1/trips/not-a-uuid", headers=HEADERS)
    assert response.status_code == 401
