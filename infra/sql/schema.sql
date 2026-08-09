-- RouteShield operational schema baseline.
--
-- Production deployments run this through tools/migrate.py before shifting Cloud
-- Run traffic. All domain documents are JSONB so Pydantic validation remains the
-- single contract boundary; tenant, status, and foreign-key columns exist for
-- indexed isolation and lifecycle queries.

CREATE TABLE IF NOT EXISTS trips (
  trip_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS trips_tenant_id_idx ON trips (tenant_id);

CREATE TABLE IF NOT EXISTS trip_segments (
  trip_id UUID NOT NULL,
  segment_id VARCHAR(64) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  carrier_code VARCHAR(3) NOT NULL,
  flight_number VARCHAR(4) NOT NULL,
  departure_airport VARCHAR(3) NOT NULL,
  arrival_airport VARCHAR(3) NOT NULL,
  scheduled_departure_at TIMESTAMPTZ NOT NULL,
  scheduled_arrival_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (trip_id, segment_id)
);
CREATE INDEX IF NOT EXISTS trip_segments_tenant_departure_idx
  ON trip_segments (tenant_id, scheduled_departure_at);

CREATE TABLE IF NOT EXISTS evidence_items (
  evidence_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  trip_id UUID NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_items_tenant_trip_idx ON evidence_items (tenant_id, trip_id);

CREATE TABLE IF NOT EXISTS risk_assessments (
  assessment_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  trip_id UUID NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS risk_assessments_tenant_trip_idx ON risk_assessments (tenant_id, trip_id);

CREATE TABLE IF NOT EXISTS incidents (
  incident_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  trip_id UUID NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_tenant_trip_idx ON incidents (tenant_id, trip_id);

CREATE TABLE IF NOT EXISTS traveler_preference_profiles (
  tenant_id VARCHAR(128) NOT NULL,
  traveler_id VARCHAR(128) NOT NULL,
  payload JSONB NOT NULL,
  PRIMARY KEY (tenant_id, traveler_id)
);

CREATE TABLE IF NOT EXISTS approvals (
  incident_id UUID PRIMARY KEY,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
  event_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_events_tenant_idx ON audit_events (tenant_id);

CREATE TABLE IF NOT EXISTS idempotency_records (
  tenant_id VARCHAR(128) NOT NULL,
  idempotency_key VARCHAR(256) NOT NULL,
  payload JSONB NOT NULL,
  PRIMARY KEY (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS original_uploads (
  original_upload_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  status VARCHAR(32) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS original_uploads_tenant_status_idx
  ON original_uploads (tenant_id, status);

CREATE TABLE IF NOT EXISTS runtime_control_overrides (
  tenant_id TEXT NOT NULL,
  control_name TEXT NOT NULL,
  payload JSONB NOT NULL,
  PRIMARY KEY (tenant_id, control_name)
);

CREATE TABLE IF NOT EXISTS runtime_control_changes (
  control_change_id UUID PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  control_name TEXT NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS runtime_control_changes_tenant_idx
  ON runtime_control_changes (tenant_id, control_name);

CREATE TABLE IF NOT EXISTS platform_runtime_control_overrides (
  control_name VARCHAR(64) PRIMARY KEY,
  payload JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_runtime_control_changes (
  control_change_id UUID PRIMARY KEY,
  control_name VARCHAR(64) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS platform_runtime_control_changes_control_idx
  ON platform_runtime_control_changes (control_name);

CREATE TABLE IF NOT EXISTS memory_update_proposals (
  proposal_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_update_proposals_tenant_idx ON memory_update_proposals (tenant_id);

CREATE TABLE IF NOT EXISTS recovery_candidate_sets (
  candidate_set_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  incident_id UUID NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS recovery_candidate_sets_tenant_incident_idx
  ON recovery_candidate_sets (tenant_id, incident_id);

CREATE TABLE IF NOT EXISTS recovery_candidate_outcomes (
  outcome_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  incident_id UUID NOT NULL,
  candidate_set_id UUID NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS recovery_candidate_outcomes_tenant_set_idx
  ON recovery_candidate_outcomes (tenant_id, candidate_set_id);

CREATE TABLE IF NOT EXISTS manager_feedback (
  feedback_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  incident_id UUID NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS manager_feedback_tenant_incident_idx
  ON manager_feedback (tenant_id, incident_id);

CREATE TABLE IF NOT EXISTS tenant_playbooks (
  playbook_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS tenant_playbooks_tenant_idx ON tenant_playbooks (tenant_id);

CREATE TABLE IF NOT EXISTS notifications (
  notification_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  status VARCHAR(32) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_tenant_status_idx ON notifications (tenant_id, status);

CREATE TABLE IF NOT EXISTS notification_attempts (
  attempt_id UUID PRIMARY KEY,
  notification_id UUID NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS notification_attempts_notification_idx ON notification_attempts (notification_id);

CREATE TABLE IF NOT EXISTS action_dispatches (
  action_dispatch_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  incident_id UUID NOT NULL,
  status VARCHAR(32) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS action_dispatches_tenant_status_idx
  ON action_dispatches (tenant_id, status);

CREATE TABLE IF NOT EXISTS action_dispatch_attempts (
  attempt_id UUID PRIMARY KEY,
  action_dispatch_id UUID NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS action_dispatch_attempts_dispatch_idx
  ON action_dispatch_attempts (action_dispatch_id);

CREATE TABLE IF NOT EXISTS memory_audit_events (
  audit_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  traveler_id VARCHAR(128) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_audit_events_tenant_traveler_idx
  ON memory_audit_events (tenant_id, traveler_id);

CREATE TABLE IF NOT EXISTS change_records (
  change_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  change_type VARCHAR(32) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS change_records_tenant_idx ON change_records (tenant_id);

CREATE TABLE IF NOT EXISTS provider_onboarding_records (
  provider_onboarding_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  status VARCHAR(32) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS provider_onboarding_records_tenant_idx
  ON provider_onboarding_records (tenant_id);

CREATE TABLE IF NOT EXISTS legal_holds (
  legal_hold_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  status VARCHAR(32) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS legal_holds_tenant_status_idx
  ON legal_holds (tenant_id, status);

CREATE TABLE IF NOT EXISTS deletion_requests (
  deletion_request_id UUID PRIMARY KEY,
  tenant_id VARCHAR(128) NOT NULL,
  traveler_id VARCHAR(128) NOT NULL,
  status VARCHAR(32) NOT NULL,
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS deletion_requests_tenant_traveler_status_idx
  ON deletion_requests (tenant_id, traveler_id, status);
