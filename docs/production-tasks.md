# RouteShield Production Task Tracker

Project author: Sarala Biswal

Status: `TODO` | `IN PROGRESS` | `BLOCKED` | `DONE`

## 1. Durable graph execution

- [x] **DONE** Wire live graph runs to `AsyncPostgresSaver` when `DATABASE_URL` is configured.
- [x] **DONE** Persist and retrieve graph thread state across API restart tests.
- [x] **DONE** Add graph resume and event-stream integration tests.

## 2. Provider integrations

- [x] **DONE** Implement the Amadeus flight-status adapter and captured fixtures.
- [x] **DONE** Implement the FAA, NWS, and AviationWeather adapters.
- [x] **DONE** Implement the Google Routes and State Department advisory adapters.
- [x] **DONE** Add Redis cache, retry, circuit-breaker, and source-freshness policies.

## 3. Recovery workflow

- [x] **DONE** Implement approved alternative-flight retrieval.
- [x] **DONE** Implement structured corporate-policy eligibility checks.
- [x] **DONE** Persist candidate sets, ranking decisions, overrides, and recovery outcomes.

## 4. API security and integration controls

- [x] **DONE** Replace role headers with OIDC/JWT validation and claims-to-role mapping.
- [x] **DONE** Add durable Redis/PostgreSQL idempotency and webhook replay protection.
- [x] **DONE** Add API rate limiting and audit-log redaction.

## 5. Web UI and notifications

- [x] **DONE** Build the manager queue, incident detail, timeline, and approval UI.
- [x] **DONE** Build the traveler preference and notification-preview UI.
- [x] **DONE** Implement the notification queue, delivery, retry, and acknowledgement records.
- [ ] **BLOCKED** Connect tenant-approved external notification-channel adapters. Requires
      notification policy approval and provider credentials/contracts.

## 6. GCP delivery

- [x] **DONE** Complete Terraform for Cloud SQL, Redis, Pub/Sub, Scheduler, Storage, VPC,
      IAM, monitoring, isolated web/monitor workloads, and an optional HTTPS edge.
- [x] **DONE** Add container build, database migration, staging smoke-test, production
      deployment, dependency-scan, and secret-scan workflows.
- [x] **DONE** Configure Secret Manager bindings and Cloud Run service identities.

## 7. Quality, privacy, and governance

- [x] **DONE** Create a versioned, 57-scenario graph/provider/model evaluation suite.
- [x] **DONE** Add load, security, tenant-isolation, queue-backpressure, and restore tests.
- [x] **DONE** Implement classification-aware retention/deletion jobs, legal holds, DSAR
      processing, and the memory-audit workflow.
- [x] **DONE** Add model/prompt/policy change records, provider onboarding records,
      operational telemetry, alerts, and runbooks.

## 8. PRD compliance remediation (ordered)

The items below were added following a PRD-to-implementation review. They distinguish
completed production capabilities from work that is still pending or blocked on external
input.

- [x] **DONE** **1. Durable mutation safety** — Persist approval state updates; add
  request-hash-bound, Redis/PostgreSQL idempotency for every mutating API and job; validate
  webhook timestamps, schemas, and message IDs and reject replays; use an outbox/claim
  pattern so retried jobs cannot duplicate notifications or actions.
  - [x] Persist PostgreSQL approval updates with an incident upsert.
  - [x] Add request-hash-bound, tenant-scoped PostgreSQL idempotency records and stable
    replay handling for approval, trip, evidence, assessment, candidate-set, memory,
    notification, and governance mutations when `REQUIRE_IDEMPOTENCY=true`.
  - [x] Validate signed booking-webhook timestamps, schema, message IDs, and tenant
    consistency, and reject replay conflicts before recording a redacted event.
  - [x] Add idempotent scheduled-job/event consumption and transactional outbox claims for
    notification/action dispatch.
    - [x] Claim each scheduled assessment-due monitoring window before assessment, and
      replay a duplicate delivery without triggering a second assessment.
    - [x] Add transactional outbox claims for notification/action dispatch. Notification and
      approved-action jobs lease a record atomically, record completion and attempt state in
      a single transaction, and require a reviewed provider adapter before any external
      action can run.
- [x] **DONE** **2. Authorization and controls** — Enforce OIDC claims at tenant, role,
  trip, incident, traveler, and assignment scope; remove the production header fallback; and
  enforce and audit every kill switch, including provider and tenant-automation controls.
  - [x] Require a signed OIDC identity in deployed environments, configure claims mapping,
    and reject header-only authentication there; platform administrators have no default
    tenant-data role.
  - [x] Add trip-manager assignment, enforce traveler ownership and manager assignment at
    trip/incident/candidate/approval/notification boundaries, and limit duty-of-care access
    to High/Critical incidents.
  - [x] Add a traveler-safe incident view that never exposes manager rationale and returns
    traveler guidance only after an approval.
  - [x] Enforce memory read/write, notification, approval-action, and tenant-automation
    controls; approvals persist with external action dispatch marked `suppressed` when
    disabled.
  - [x] Audit the effective runtime and provider kill-switch state at each API start.
  - [x] Persist tenant-administrator control changes with actor, scope, reason, prior/new
    values, and expiry/review time, and apply the approved override without a redeploy.
  - [x] Apply tenant provider controls in live-adapter routing before provider requests.
  - [x] Add the corresponding platform-administrator control plane without granting platform
    operators tenant-data access. Platform defaults are stored and audited separately from
    tenant overrides, and platform roles remain denied by tenant-data endpoints.
- [ ] **BLOCKED** **3. Real ingest, evidence, and monitoring** — Store and quarantine
  original uploads; normalize trips/segments; implement webhook ingestion; invoke configured
  live adapters (including FAA); and consume scheduled assessment-due events idempotently.
  - [x] Replace deployed fixture collection with bounded live Amadeus, FAA, NWS,
    AviationWeather, Google Routes, and advisory adapters; disabled sources produce
    structured unavailable evidence, and tenant provider overrides apply before any provider
    request.
  - [x] Add Cloud Scheduler/Pub/Sub assessment-due delivery and an idempotent consumer for
    the 24-hour, 6-hour, and 2-hour pre-departure windows.
  - [x] Support a signed `itinerary.upsert` booking-webhook contract that validates
    tenant-bound trips, persists the normalized itinerary idempotently, and starts its
    baseline assessment.
  - [x] Quarantine original CSV bytes in tenant-prefixed Cloud Storage objects in deployed
    environments; retain only checksummed, validation-state metadata in PostgreSQL.
  - [x] Normalize flight segments into indexed relational records while retaining the
    existing trip document as the API compatibility representation.
  - [x] Add canonical `itinerary.updated` and `itinerary.cancelled` contracts; validated
    updates replace normalized segments and refresh evidence, while cancellations persist
    trip state, add booking-status evidence, and trigger reassessment.
  - [ ] **BLOCKED** Map each contracted provider's native change/cancellation payload into
    the canonical itinerary contracts once those provider schemas are approved.
- [x] **DONE** **4. Production graph, recovery, and memory** — Implement the complete graph
  topology, persist graph recommendations/state, integrate policy-eligible alternatives and
  outcome-rich candidate sets, load scoped preferences, and add manager-feedback/playbook
  support.
  - [x] Persist graph-produced, evidence-grounded recommendations on the incident, and
    include the original itinerary, assessment, evidence, policy placeholder, tool audit, and
    proposed action payload in the approval interrupt.
  - [x] Add policy-eligible alternatives and outcome-rich candidate-set state. The graph now
    evaluates every tool-returned alternative against server-owned policy before
    deterministic ranking, persists an immutable candidate snapshot with display positions,
    and records append-only offered/viewed/selected/rejected/completed outcomes.
  - [x] Load confirmed tenant/traveler-scoped preferences into graph state and add
    manager-feedback/playbook support. Preferences are allow-listed, tenant/traveler scoped,
    and can only tighten an otherwise eligible option; manager feedback and recovery outcomes
    are stored separately from traveler memory, and versioned tenant-admin playbooks remain
    approval-only, read-only guidance.
  - [x] Add an explicit graph policy-gate/resume-dispatch node so the durable approved-action
    outbox is represented in checkpointed graph state, not only at the API boundary.
- [ ] **BLOCKED** **5. Notification and experience completion** — Add tenant-approved
  external channel adapters, transactional delivery claims, traveler channel/acknowledgement
  preferences, accessible dashboard filtering/metrics/freshness, and airport-local-time
  display.
  - [x] Add transactional notification delivery claims and an explicit, audited traveler
    channel-preference settings API.
  - [ ] **BLOCKED** Add external channel adapters, pending tenant notification policy and
    provider credentials/contract approval.
  - [x] Add accessible dashboard filtering, metrics/freshness, and airport-local-time
    display.
- [x] **DONE** **6. Audit, privacy, and operations** — Capture correlated
  model/provider/tool/memory/action telemetry; implement retention by classification, legal
  holds, DSAR scope, backup expiry, and restore drills; and add required alerts and complete
  operational runbooks.
  - [x] Emit redacted, correlation-safe structured telemetry for assessment, provider, graph,
    recommendation, notification, action, memory, and privacy events.
  - [x] Add audited, scoped legal holds and idempotent deletion-request processing that
    blocks on active holds; apply separate original-upload and audit retention windows.
  - [x] Add Cloud Monitoring provider/backlog/notification/privacy alerts and outage,
    rollback, privacy, prompt-injection, notification, and restore runbooks.
- [x] **DONE** **7. Delivery and verification gates** — Add separate web/monitor workloads,
  HTTPS/edge architecture, telemetry, secret/dependency scanning, rollback drills, capacity
  and queue tests, and the full 40–60-scenario/malformed-output/prompt-injection evaluation
  suite.
  - [x] Add an isolated, no-secret web service, a monitor Cloud Run Job, Scheduler job
    invocation, and an optional domain-backed HTTPS load-balancer edge.
  - [x] Add CI secret/dependency scanning and 57 versioned release scenarios, plus concurrent
    queue lease/backpressure coverage.

Terraform apply/validation and production smoke execution remain gated only by the external
environment inputs below; CI installs Terraform for formatting and validation.

## External inputs required before production deployment

- [ ] **BLOCKED** GCP project ID, region, billing account, domain, and IAM owners.
- [ ] **BLOCKED** VPC/Cloud SQL connectivity decision.
- [ ] **BLOCKED** OpenAI service key and provider credentials in Secret Manager.
- [ ] **BLOCKED** OIDC issuer, audience, JWKS URL, and claims mapping.
- [ ] **BLOCKED** Provider contracts, quotas, notification policy, retention policy, and
      privacy approvals.
