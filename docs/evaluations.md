# Evaluation and release gates

Project author: Sarala Biswal

This document describes the evidence required to release RouteShield safely. It distinguishes
deterministic checks that run in CI from validation that must run in a deployed staging environment.
It is not a live-model benchmark or a production-operations dashboard.

## Automated CI gates

Every pull request and change to `main` runs the following gates through
`.github/workflows/ci.yml`:

1. Committed-secret scan (`gitleaks`).
2. Locked Python dependency audit (`pip-audit`).
3. Dependency installation from `uv.lock`.
4. Ruff linting and the complete `pytest` suite.
5. Container-image build.
6. Terraform formatting and offline configuration validation.

Run the core evaluation checks locally with:

```bash
uv run pytest tests/test_evaluations.py tests/test_capacity.py
```

Run the complete release-test suite with:

```bash
uv run ruff check .
uv run pytest
```

## Deterministic golden suite

`tools/evaluation.py` defines the versioned golden-scenario catalog. The current version is
`2026.07.1` and contains **57** named cases, which is within the PRD requirement of 40–60 cases:

| Evidence profile | Cases | Expected disposition |
|---|---:|---|
| `normal` | 12 | Monitor |
| `disruption` | 26 | Investigate |
| `source_outage` | 19 | Needs human review |

The catalog covers normal operation, disruption, source outages, malformed model output,
prompt-injection attempts, memory boundaries, approval/replay safety, notification/action controls,
retention, and legal holds. Case IDs are a versioned product-behavior taxonomy; they make the
reason each case exists visible in code review and governance records.

The automated suite uses recorded fixture evidence and verifies:

- provider-envelope completeness: source identity, evidence reference, retrieval/expiry ordering;
- deterministic risk score, severity, and disposition routing for the fixture profile;
- source-health escalation when core evidence is unavailable;
- recommendation evidence IDs are present in the evidence set;
- ranked alternatives are returned only from the approved candidate set; and
- recommendations explicitly require human approval.

### Important scope boundary

The 57 case IDs are not 57 independently executed end-to-end model conversations. The current
deterministic evaluator maps them to three fixture evidence profiles and verifies their expected
routing. It deliberately does **not** invoke OpenAI or any live provider.

This distinction is intentional: CI must be reproducible, credential-free, and safe. The scenario
catalog is still the release contract, but individual API, graph, memory, webhook, privacy, and
provider controls are exercised by the dedicated test modules below.

## Coverage by test level

| Test level | Primary tests | What is verified |
|---|---|---|
| Deterministic evaluation | `test_evaluations.py`, `tools/evaluation.py` | Fixture envelopes, scoring, severity, routing, evidence grounding, approval-only recommendations |
| Risk and policy | `test_risk_engine.py`, `test_policies.py`, `test_recovery.py` | Weighted risk score, policy eligibility, recovery ordering, constraints |
| API and ingestion | `test_api.py`, `test_ingestion.py`, `test_idempotency.py`, `test_webhook_security.py` | Tenant-scoped API behavior, validation, replay protection, signed webhooks |
| Graph and model boundary | `test_graph_contract.py`, `test_openai_provider.py` | Tool allow-list, bounded workflow contract, runtime model control, redacted model-audit metadata, and structured model-provider boundary |
| Provider and resilience | `test_amadeus.py`, `test_weather_sources.py`, `test_providers.py`, `test_route_advisory_sources.py`, `test_operational_resilience.py` | Normalized adapters, fixture failures, freshness, retry and degraded-mode behavior |
| Security and controls | `test_auth.py`, `test_security.py`, `test_production_controls.py` | OIDC controls, redaction, authorization, kill switches, tenant isolation |
| Memory and privacy | `test_api.py`, `test_production_controls.py` | Consent, scope, confirmation, retention, deletion requests, legal holds |
| Queue capacity | `test_capacity.py` | 1,000 notification records receive one unique concurrent lease each in the in-memory repository |

## Staging-only release evidence

The following are not proven by local fixtures or the in-memory capacity test. Record the result
in the release or governance change record before production deployment:

- Cloud Run connectivity to private Cloud SQL and Memorystore, including restart/checkpoint
  continuity and Cloud SQL pool behavior.
- Cloud Storage quarantine writes, Pub/Sub delivery, Scheduler invocation, migration-job execution,
  and monitoring/dead-letter alerts.
- Corporate OIDC token validation, tenant and role claim mapping, and service-account IAM scope.
- Approved live-provider credentials, quotas, timeouts, freshness, retry/circuit-breaker telemetry,
  and safe degraded-mode behavior.
- The deployed 1,000 assessments/hour capacity target and notification-worker backpressure.
- Custom-domain TLS, CORS, and browser-console behavior when the optional HTTPS edge is enabled.

## Model, prompt, and policy changes

The current automated model checks validate output safety, tool-call validation, and grounding;
they do not measure the quality of a live OpenAI response. LLM and live-provider features remain
disabled by default in the deployment configuration until explicitly approved. Local Compose uses
a credential-free mock provider to exercise the HTTP-adapter path, not a live provider.

Before enabling or changing a model, prompt, tool schema, or risk policy:

1. Create a governance change record with the owner, reason, prior and proposed versions, risk
   assessment, approver, and rollback version.
2. Run the deterministic suite and relevant dedicated tests; attach the results.
3. Compare the change against the golden-scenario contract. It must not weaken evidence citation,
   policy eligibility, tenant isolation, human approval, or safe fallback behavior.
4. Run an approved staging evaluation with the selected dated model snapshot. Record structured
   output validity, citation accuracy, recommendation relevance, latency, cost, and any failure or
   fallback result.
5. Obtain the required product, travel-operations, platform, security, and privacy approvals before
   production rollout.

The PRD targets for alert precision/recall, time to recommendation, and first-pass structured-output
validity are not currently calculated by an automated metrics job. Treat them as staging/production
measurement requirements, not as claims made by this deterministic CI suite.

## Maintaining the suite

Add a case when a production incident, provider change, security finding, or approved manager
feedback reveals a behavior that is not already covered. Each new or changed case must include a
stable ID, evidence profile or dedicated fixture, expected disposition, owner, and governance
reference. Update `EVALUATION_SUITE_VERSION` when the release contract changes.

Do not turn a missing outcome into a negative label, add production personal data to fixtures, or
weaken a safety assertion to make a model change pass. The future Learned Recovery Ranker has its
own Phase 2 evaluation and promotion gates in the PRD.
