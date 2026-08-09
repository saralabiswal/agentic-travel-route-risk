# RouteShield Production Runbooks

Project author: Sarala Biswal

These runbooks define the operating boundary for RouteShield. They do not replace the
approvals listed in `production-tasks.md`.

## GCP environment bootstrap and staged deployment

Perform this procedure independently for `staging` and `production`. Complete and validate
staging before creating or changing the production environment.

### 1. Obtain the required decisions and approvals

1. Create a dedicated GCP project, link its billing account, select a region, and assign
   named platform, security, travel-operations, and privacy owners. Use separate projects
   for staging and production.
2. Approve the VPC and private Cloud SQL connectivity design, the retention periods, and the
   privacy/DSAR process.
3. Obtain the corporate OIDC issuer, audience, JWKS URL, and approved claims mapping.
   RouteShield expects an actor claim, a tenant claim, and a role claim; the deployment
   defaults are `sub`, `tenant_id`, and `role`.
4. Complete provider onboarding before enabling a live source: contract, quota owner,
   purpose, data classification, security review, and fallback plan. Do not enable external
   notifications until the tenant notification policy and provider contract are approved.

### 2. Bootstrap cloud administration and CI access

1. Create an access-restricted GCS bucket for Terraform state. It must exist before
   Terraform can initialize the `gcs` backend. Never use an evidence, audit, or
   application-data bucket as the state bucket.
2. Create a dedicated GitHub deployment service account and configure GitHub Actions
   Workload Identity Federation. Restrict the federation attribute condition to this
   repository and its approved deployment branch or environment; do not create or store a
   downloadable service-account key.
3. Grant the deployer only the project-level permissions needed to enable APIs and manage
   the Terraform-owned resources. Review and reduce bootstrap privileges after initial
   provisioning.
4. In the GitHub `staging` and `production` environments, configure these variables:

   ```text
   GCP_PROJECT_ID
   GCP_REGION
   TF_STATE_BUCKET
   WEB_DOMAIN                 # optional; enables the HTTPS load balancer
   WEB_API_BASE_URL           # optional until the web edge is configured
   OIDC_ISSUER
   OIDC_AUDIENCE
   OIDC_JWKS_URL
   ```

5. In the same GitHub environments, configure these secrets:

   ```text
   GCP_WORKLOAD_IDENTITY_PROVIDER
   GCP_DEPLOYER_SERVICE_ACCOUNT
   ```

   Do not commit a real `terraform.tfvars`, project identifier, credential, or provider key.

### 3. Create the runtime secret values

Terraform creates the Secret Manager secret *containers* and access bindings, but it
deliberately does not create secret values. The deployed API, migration job, and monitor job
require versions for all of the following before the full Cloud Run deployment:

```text
OPENAI_API_KEY
DATABASE_URL
REDIS_URL
BOOKING_WEBHOOK_SECRET
```

There is an initial bootstrap dependency to resolve before using the standard deployment
workflow: the current Terraform creates Cloud SQL and Memorystore but does not create a
PostgreSQL application user, derive the connection strings, or add Secret Manager versions.
An operator must complete the following after the database and Redis instances exist, and
before the first full Cloud Run apply:

1. Create a dedicated PostgreSQL application user with only the privileges required by the
   API and the additive migration process. Store its password only through the approved
   secret-handling process.
2. Obtain the Cloud SQL private IP address and construct the async SQLAlchemy connection
   string for the `routeshield` database. Validate it from the same VPC path that Cloud Run
   will use.
3. Validate the Memorystore TLS connection and construct the Redis URL. If Redis AUTH is
   enabled, retrieve the generated AUTH string through an approved administrative path and
   store it only in Secret Manager.
4. Add versions for the four runtime secrets. Their Terraform-created names are
   `routeshield-<environment>-database_url`, `routeshield-<environment>-redis_url`,
   `routeshield-<environment>-openai_api_key`, and
   `routeshield-<environment>-booking_webhook_secret`.
5. Confirm that the API and monitor service identities hold `Secret Manager Secret
   Accessor` only for the secrets they require.

Do not work around this prerequisite by committing values to Terraform or application
environment files. Use a one-time, targeted infrastructure bootstrap or an equivalent
reviewed operator procedure to create the database, Redis instance, and secret containers
before adding the secret versions. Normal releases must use the complete Terraform plan, not
targeted applies.

### 4. Deploy and validate staging

1. Run the CI quality gates. They must pass Ruff, tests, secret/dependency scans, the
   container build, and Terraform formatting/validation.
2. After the staging environment variables, secrets, runtime secret values, and approvals
   above are in place, trigger **Deploy staging** manually from the GitHub Actions tab. It
   provisions Artifact Registry when needed, builds and pushes an immutable image, applies
   Terraform, runs the additive migration Cloud Run Job, and calls the authenticated
   `/health` endpoint. Pushes to `main` run CI only; they never attempt a cloud deployment.
3. Verify that the API and console use OIDC tokens with correct tenant and role claims;
   header-only development authentication must not work in this environment.
4. Verify Cloud SQL persistence, Redis rate limiting, Cloud Storage upload quarantine,
   Pub/Sub and Scheduler delivery, migration/restart continuity, Cloud Monitoring alerts, and
   the source-outage path. Keep live providers, model calls, external notifications, and
   approval actions disabled until their corresponding approvals and staging tests are
   complete.
5. If a custom web domain is approved, create its DNS A record using the Terraform
   `web_edge_ip` output, wait for the managed certificate to become active, then verify the
   web console's API origin and CORS behavior.

### 5. Promote to production

1. Repeat the bootstrap and secret procedure in the separate production project.
2. Confirm the staging deployment passed its quality gates and authenticated `/health`
   smoke test.
3. Run **Deploy production** with a previously tested immutable image digest or tag.
   GitHub production-environment approval is mandatory; review the generated Terraform plan
   before applying it.
4. Verify that the migration Cloud Run Job completed. For at least 30 minutes, review Cloud
   Run 5xx errors, Cloud SQL CPU, provider availability, and queue/dead-letter alerts.
5. To roll back, rerun **Deploy production** with the previous immutable image. Additive
   database migrations are deliberately not automatically reversed; a destructive rollback
   requires its own approved migration and restore plan.

### 6. Before enabling a new runtime capability

1. Enable one live provider at a time in staging and verify its timeout, retry,
   circuit-breaker, freshness, and degraded-mode behavior.
2. Before enabling the OpenAI/ReAct runtime, record the model, prompt, evaluation evidence,
   risk assessment, approver, and rollback plan.
3. Before enabling an external notification channel or action dispatcher, obtain the
   applicable provider contract, privacy approval, policy approval, credentials, and an
   end-to-end delivery test.

## Incident and notification operations

1. Open `/console/` through the authenticated Cloud Run endpoint.
2. Review the incident evidence, source freshness, recommendation, and timeline. Approval is
   a recorded decision, never a booking action.
3. Preview the traveler message before queueing it. Queue records contain opaque recipient
   references; provider adapters must not receive raw contact details from the console.
4. The delivery worker records every attempt. `retry_scheduled` backs off exponentially and
   is capped; after five attempts the record becomes `failed`. Resolve a failed provider
   configuration before requeueing a newly approved notification.
5. Acknowledgements are explicit records. Do not infer acknowledgement from provider
   delivery.

## Privacy, memory, and retention

1. Preference changes always enter as consent-required proposals. The memory audit endpoint
   retains only action, actor, proposal, and changed-field names — not the preference values.
2. Use `DELETE /v1/travelers/{traveler_id}/preferences` for a preference-memory erasure
   request. It removes the profile and proposals and writes a minimal deletion audit event.
3. Cloud Scheduler invokes the retention job daily. Quarantined originals expire after 30
   days, operational evidence after the configured `RETENTION_DAYS`, and security audit
   archives after Terraform's `audit_retention_days` setting (365 by default). The worker
   reports aggregate deletion counts and never logs deleted payloads.
4. Create a scoped hold with `POST /v1/privacy/legal-holds` before an investigation,
   litigation, or regulatory preservation requirement. A hold can cover only a tenant,
   traveler, or trip; it is auditable and stops both automated retention and
   deletion-request processing for its scope.
5. Submit a DSAR-style request with `POST /v1/privacy/deletion-requests`. The daily
   processor erases profile memory and, for `traveler_data`, associated mutable trips,
   evidence, incidents, notifications, and access links. It retains only minimal
   security-audit records. A hold changes the request to `blocked_by_legal_hold`; release
   the hold through its audited release endpoint before retrying processing.
6. Do not configure a final retention value or release a legal hold without the approved
   privacy policy. The defaults are implementation safeguards, not a substitute for that
   approval.

## Provider onboarding

1. Create a provider-onboarding record before enabling any provider environment flag.
2. Attach the executed contract, quota reference, purpose, data classification, and
   accountable owner; move it through security review and approval.
3. Store provider credentials only in Secret Manager, bind them to the API service account,
   and verify freshness, timeout, retry, and circuit-breaker telemetry in staging.
4. If the provider is degraded, disable its feature flag. The assessment must expose
   unavailable evidence and route to human review when core visibility is insufficient.

## Restore exercise

1. At least quarterly, restore a Cloud SQL point-in-time backup into an isolated
   project/VPC.
2. Apply `tools/migrate.py`, load a tenant-scoped golden scenario, and verify that no
   records from a second tenant are readable.
3. Compare incident/evidence counts, notification attempts, memory-audit records, and schema
   version with the restoration target. Record the evidence in the change register.
4. Destroy the isolated restore environment after approval. Never direct a restore at
   production.

## Provider or OpenAI outage

1. Confirm the provider-unavailable alert, source freshness, and correlation ID in the
   incident timeline. Do not infer a low-risk result from missing evidence.
2. Disable only the affected provider/runtime control when necessary; preserve the incident
   and route assessments with insufficient core visibility to human review.
3. For an OpenAI outage or rate limit, leave deterministic scoring and policy checks
   running, retain the safe fallback recommendation, and do not retry past the configured
   bounded model attempts.
4. Record recovery, evidence freshness, and any control change in the tenant control
   history.

## Stuck approval, failed notification, or action dispatch

1. Find the incident by correlation ID and inspect its checkpoint, approval state, and
   outbox attempt history. Never manually invoke a provider action from a console or
   database shell.
2. A paused approval may be resumed only through the approval endpoint, using its original
   idempotency contract. A replay must return the stored outcome, not create another action.
3. For failed notifications or unconfigured external channels, correct the approved provider
   configuration, then create a newly approved notification; the existing attempt history
   remains immutable. External adapters remain disabled until their policy and contract are
   approved.

## Incorrect policy, memory access, or prompt injection

1. Disable the relevant tenant or platform control and capture the correlation ID,
   policy/model version, affected scope, and audit record.
2. For a policy release, use the documented rollback plan in its governance change record,
   rerun the golden suite, and obtain approval before re-enabling automation.
3. For suspected cross-tenant or unconsented memory access, stop memory reads/writes for the
   affected tenant, preserve a narrowly scoped legal hold if required, investigate audit
   records, and process any approved deletion request.
4. Treat prompt-injection content as untrusted data. Preserve only redacted evidence
   references; never move it into privileged instructions, a playbook, or a memory profile.
