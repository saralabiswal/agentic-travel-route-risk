terraform {
  required_version = ">= 1.6"

  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "project_id" {
  type        = string
  description = "GCP project that owns the RouteShield environment."
}

variable "region" {
  type        = string
  description = "Regional location for all RouteShield resources."
  default     = "us-central1"
}

variable "environment" {
  type        = string
  description = "Deployment environment name."
  default     = "staging"
}

variable "image" {
  type        = string
  description = "Immutable container image URI (prefer a digest)."
}

variable "web_image" {
  type        = string
  description = "Optional web-console image URI. Defaults to the API image when empty."
  default     = ""
}

variable "web_api_base_url" {
  type        = string
  description = "Browser-safe public API base URL for the isolated web service."
  default     = ""
}

variable "web_domain" {
  type        = string
  description = "Approved public web DNS name. Enables the HTTPS load-balancer edge when set."
  default     = ""
}

variable "oidc_issuer" {
  type        = string
  description = "Corporate OIDC issuer URL used by the API."
}

variable "oidc_audience" {
  type        = string
  description = "Audience expected in corporate OIDC access tokens."
}

variable "oidc_jwks_url" {
  type        = string
  description = "JWKS URL used to verify corporate OIDC access tokens."
}

variable "runtime_invoker_members" {
  type        = set(string)
  description = "IAM members allowed to invoke the authenticated Cloud Run service."
  default     = []
}

variable "notification_channels" {
  type        = list(string)
  description = "Optional existing Cloud Monitoring notification-channel resource names."
  default     = []
}

variable "min_instances" {
  type        = number
  default     = 0
}

variable "max_instances" {
  type        = number
  default     = 10
}

variable "sql_tier" {
  type        = string
  default     = "db-custom-2-7680"
}

variable "redis_memory_size_gb" {
  type        = number
  default     = 1
}

variable "retention_days" {
  type        = number
  description = "Approved record-retention period. Override only after privacy approval."
  default     = 90
}

variable "audit_retention_days" {
  type        = number
  description = "Security-audit retention period; must meet the approved minimum."
  default     = 365
}

variable "deletion_protection" {
  type        = bool
  description = "Protect Cloud SQL from accidental destroy in non-ephemeral environments."
  default     = true
}

locals {
  name_prefix = "routeshield-${var.environment}"
  effective_web_image = var.web_image != "" ? var.web_image : var.image
  labels = {
    application = "routeshield"
    environment = var.environment
    managed-by  = "terraform"
  }
  required_services = toset([
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "cloudscheduler.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "redis.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "vpcaccess.googleapis.com",
  ])
  runtime_secrets = {
    database_url           = "DATABASE_URL"
    redis_url              = "REDIS_URL"
    openai_api_key         = "OPENAI_API_KEY"
    booking_webhook_secret = "BOOKING_WEBHOOK_SECRET"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "current" {}

resource "google_project_service" "required" {
  for_each           = local.required_services
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "private" {
  name                    = "${local.name_prefix}-vpc"
  auto_create_subnetworks = false
  project                 = var.project_id
}

resource "google_compute_subnetwork" "private" {
  name          = "${local.name_prefix}-subnet"
  project       = var.project_id
  region        = var.region
  network       = google_compute_network.private.id
  ip_cidr_range = "10.42.0.0/24"
}

resource "google_compute_global_address" "private_service_access" {
  name          = "${local.name_prefix}-private-services"
  project       = var.project_id
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.private.id
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.private.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_service_access.name]

  depends_on = [google_project_service.required]
}

resource "google_vpc_access_connector" "serverless" {
  name          = "${local.name_prefix}-connector"
  project       = var.project_id
  region        = var.region
  network       = google_compute_network.private.name
  ip_cidr_range = "10.42.16.0/28"
  min_instances = 2
  max_instances = 3

  depends_on = [google_project_service.required]
}

resource "google_sql_database_instance" "primary" {
  name                = "${local.name_prefix}-postgres"
  project             = var.project_id
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = var.deletion_protection

  settings {
    tier              = var.sql_tier
    availability_type = "REGIONAL"
    disk_autoresize   = true
    disk_type         = "PD_SSD"
    user_labels       = local.labels

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "03:00"
    }

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.private.id
    }

    insights_config {
      query_insights_enabled  = true
      query_plans_per_minute  = 5
      query_string_length     = 1024
      record_application_tags = true
    }
  }

  depends_on = [google_service_networking_connection.private_services]
}

resource "google_sql_database" "application" {
  name     = "routeshield"
  instance = google_sql_database_instance.primary.name
  project  = var.project_id
}

resource "google_redis_instance" "cache" {
  name               = "${local.name_prefix}-redis"
  project            = var.project_id
  region             = var.region
  tier               = "STANDARD_HA"
  memory_size_gb     = var.redis_memory_size_gb
  redis_version      = "REDIS_7_0"
  display_name       = "RouteShield ${var.environment} cache"
  authorized_network = google_compute_network.private.id
  transit_encryption_mode = "SERVER_AUTHENTICATION"
  labels             = local.labels

  depends_on = [google_service_networking_connection.private_services]
}

resource "google_storage_bucket" "evidence" {
  name                        = "${var.project_id}-${local.name_prefix}-evidence"
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
  labels                      = local.labels

  versioning { enabled = true }

  lifecycle_rule {
    condition { age = var.retention_days }
    action { type = "Delete" }
  }

  lifecycle_rule {
    condition {
      age            = 30
      matches_prefix = ["quarantine/"]
    }
    action { type = "Delete" }
  }
}

resource "google_storage_bucket" "audit_archive" {
  name                        = "${var.project_id}-${local.name_prefix}-audit"
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
  labels                      = local.labels

  lifecycle_rule {
    condition { age = var.audit_retention_days }
    action { type = "Delete" }
  }
}

resource "google_artifact_registry_repository" "containers" {
  location      = var.region
  project       = var.project_id
  repository_id = "routeshield"
  description   = "RouteShield application containers"
  format        = "DOCKER"
  labels        = local.labels

  depends_on = [google_project_service.required]
}

resource "google_pubsub_topic" "assessment_due" {
  name    = "${local.name_prefix}-assessment-due"
  project = var.project_id
  labels  = local.labels
}

resource "google_pubsub_topic" "notification_delivery" {
  name    = "${local.name_prefix}-notification-delivery"
  project = var.project_id
  labels  = local.labels
}

resource "google_pubsub_topic" "notification_dead_letter" {
  name    = "${local.name_prefix}-notification-dlq"
  project = var.project_id
  labels  = local.labels
}

resource "google_pubsub_topic" "retention" {
  name    = "${local.name_prefix}-retention"
  project = var.project_id
  labels  = local.labels
}

resource "google_pubsub_subscription" "assessment_due" {
  name    = "${local.name_prefix}-assessment-due"
  project = var.project_id
  topic   = google_pubsub_topic.assessment_due.id

  ack_deadline_seconds       = 60
  message_retention_duration = "604800s"
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.api.uri}/v1/internal/assessments/due"
    oidc_token { service_account_email = google_service_account.scheduler.email }
  }
}

resource "google_pubsub_subscription" "notification_delivery" {
  name    = "${local.name_prefix}-notification-delivery"
  project = var.project_id
  topic   = google_pubsub_topic.notification_delivery.id

  ack_deadline_seconds       = 30
  message_retention_duration = "604800s"
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.notification_dead_letter.id
    max_delivery_attempts = 5
  }

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.api.uri}/v1/internal/notifications/dispatch"
    oidc_token { service_account_email = google_service_account.scheduler.email }
  }
}

resource "google_service_account" "api" {
  account_id   = "${local.name_prefix}-api"
  display_name = "RouteShield API (${var.environment})"
  project      = var.project_id
}

resource "google_service_account" "scheduler" {
  account_id   = "${local.name_prefix}-scheduler"
  display_name = "RouteShield Scheduler (${var.environment})"
  project      = var.project_id
}

resource "google_service_account" "web" {
  account_id   = "${local.name_prefix}-web"
  display_name = "RouteShield web (${var.environment})"
  project      = var.project_id
}

resource "google_service_account" "monitor" {
  account_id   = "${local.name_prefix}-monitor"
  display_name = "RouteShield monitor (${var.environment})"
  project      = var.project_id
}

resource "google_storage_bucket_iam_member" "api_original_upload_writer" {
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_service_account_iam_member" "pubsub_push_token_creator" {
  service_account_id = google_service_account.scheduler.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "dead_letter_publisher" {
  project = var.project_id
  topic    = google_pubsub_topic.notification_dead_letter.name
  role     = "roles/pubsub.publisher"
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription_iam_member" "delivery_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.notification_delivery.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription_iam_member" "assessment_due_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.assessment_due.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_project_iam_member" "api_cloudsql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "api_pubsub" {
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "monitor_cloudsql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.monitor.email}"
}

resource "google_project_iam_member" "monitor_pubsub" {
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.monitor.email}"
}

resource "google_storage_bucket_iam_member" "api_evidence" {
  bucket = google_storage_bucket.evidence.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret" "runtime" {
  for_each  = local.runtime_secrets
  secret_id = "${local.name_prefix}-${lower(each.value)}"
  project   = var.project_id
  labels    = local.labels

  replication { auto {} }
}

resource "google_secret_manager_secret_iam_member" "api" {
  for_each  = google_secret_manager_secret.runtime
  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "monitor" {
  for_each  = google_secret_manager_secret.runtime
  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.monitor.email}"
}

resource "google_cloud_run_v2_service" "api" {
  name     = "${local.name_prefix}-api"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.api.email
    timeout         = "60s"

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    vpc_access {
      connector = google_vpc_access_connector.serverless.id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = var.image

      ports { container_port = 8080 }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      env { name = "OIDC_ISSUER" value = var.oidc_issuer }
      env { name = "OIDC_AUDIENCE" value = var.oidc_audience }
      env { name = "OIDC_JWKS_URL" value = var.oidc_jwks_url }
      env { name = "OIDC_ACTOR_ID_CLAIM" value = "sub" }
      env { name = "OIDC_TENANT_ID_CLAIM" value = "tenant_id" }
      env { name = "OIDC_ROLE_CLAIM" value = "role" }
      env { name = "REQUIRE_OIDC" value = "true" }
      env { name = "RATE_LIMIT_REDIS_REQUIRED" value = "true" }
      env { name = "IDEMPOTENCY_TTL_SECONDS" value = "86400" }
      env { name = "REQUIRE_IDEMPOTENCY" value = "true" }
      env { name = "TENANT_AUTOMATION_ENABLED" value = "true" }
      env { name = "DEMO_EVIDENCE_ENABLED" value = "false" }
      env { name = "LLM_ENABLED" value = "false" }
      env { name = "REACT_TOOL_CALLS_ENABLED" value = "false" }
      env { name = "NOTIFICATIONS_ENABLED" value = "false" }
      env { name = "APPROVAL_ACTIONS_ENABLED" value = "false" }
      env { name = "MEMORY_READS_ENABLED" value = "true" }
      env { name = "MEMORY_WRITES_ENABLED" value = "true" }
      env { name = "RETENTION_DAYS" value = tostring(var.retention_days) }
      env { name = "EVIDENCE_BUCKET" value = google_storage_bucket.evidence.name }
      env { name = "ASSESSMENT_DUE_TOPIC" value = google_pubsub_topic.assessment_due.id }
      env { name = "NOTIFICATION_DELIVERY_TOPIC" value = google_pubsub_topic.notification_delivery.id }
      env {
        name  = "WEB_ORIGIN"
        value = var.web_domain != "" ? "https://${var.web_domain}" : ""
      }

      dynamic "env" {
        for_each = local.runtime_secrets
        content {
          name = upper(env.value)
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.runtime[env.key].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  labels = local.labels

  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.api,
  ]
}

resource "google_cloud_run_v2_service" "web" {
  name     = "${local.name_prefix}-web"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.web.email
    timeout         = "30s"

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    containers {
      image    = local.effective_web_image
      command  = [".venv/bin/python", "-m", "uvicorn"]
      args     = ["apps.web.server:app", "--host", "0.0.0.0", "--port", "8080"]

      ports { container_port = 8080 }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      env { name = "PUBLIC_API_BASE_URL" value = var.web_api_base_url }
    }
  }

  labels     = local.labels
  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_job" "migrate" {
  name     = "${local.name_prefix}-migrate"
  location = var.region
  project  = var.project_id

  template {
    template {
      service_account = google_service_account.api.email
      timeout         = "600s"
      max_retries     = 1

      vpc_access {
        connector = google_vpc_access_connector.serverless.id
        egress    = "PRIVATE_RANGES_ONLY"
      }

      containers {
        image   = var.image
        command = [".venv/bin/python"]
        args    = ["tools/migrate.py"]

        dynamic "env" {
          for_each = local.runtime_secrets
          content {
            name = upper(env.value)
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.runtime[env.key].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  labels = local.labels

  depends_on = [google_secret_manager_secret_iam_member.api]
}

resource "google_cloud_run_v2_job" "monitor" {
  name     = "${local.name_prefix}-monitor"
  location = var.region
  project  = var.project_id

  template {
    template {
      service_account = google_service_account.monitor.email
      timeout         = "900s"
      max_retries     = 1

      vpc_access {
        connector = google_vpc_access_connector.serverless.id
        egress    = "PRIVATE_RANGES_ONLY"
      }

      containers {
        image   = var.image
        command = [".venv/bin/python"]
        args    = ["-m", "workers.monitor"]

        env { name = "REQUIRE_OIDC" value = "true" }
        env { name = "RATE_LIMIT_REDIS_REQUIRED" value = "true" }
        env { name = "REQUIRE_IDEMPOTENCY" value = "true" }
        env { name = "TENANT_AUTOMATION_ENABLED" value = "true" }
        env { name = "DEMO_EVIDENCE_ENABLED" value = "false" }
        env { name = "LLM_ENABLED" value = "false" }
        env { name = "REACT_TOOL_CALLS_ENABLED" value = "false" }
        env { name = "RETENTION_DAYS" value = tostring(var.retention_days) }

        dynamic "env" {
          for_each = local.runtime_secrets
          content {
            name = upper(env.value)
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.runtime[env.key].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  labels = local.labels

  depends_on = [google_secret_manager_secret_iam_member.monitor]
}

resource "google_cloud_run_v2_service_iam_member" "runtime_invokers" {
  for_each = var.runtime_invoker_members
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = each.value
}

resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_run_v2_service_iam_member" "api_public_invoker" {
  # Cloud Run permits the request through; FastAPI requires and validates the
  # corporate OIDC token before every browser/partner API route.
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "web_public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.web.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_job_iam_binding" "scheduler_monitor_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.monitor.name
  role     = "roles/run.invoker"
  members  = ["serviceAccount:${google_service_account.scheduler.email}"]
}

resource "google_project_iam_member" "scheduler_pubsub" {
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "assessment_due" {
  name        = "${local.name_prefix}-assessment-due"
  project     = var.project_id
  region      = var.region
  schedule    = "*/15 * * * *"
  time_zone   = "Etc/UTC"
  description = "Publishes assessment-due checks; consumers claim idempotency keys."

  pubsub_target {
    topic_name = google_pubsub_topic.assessment_due.id
    data       = base64encode(jsonencode({ source = "cloud-scheduler", type = "assessment_due" }))
  }
}

resource "google_cloud_scheduler_job" "monitor" {
  name        = "${local.name_prefix}-monitor"
  project     = var.project_id
  region      = var.region
  schedule    = "*/15 * * * *"
  time_zone   = "Etc/UTC"
  description = "Runs isolated scheduled assessment processing in the monitor Cloud Run Job."

  http_target {
    http_method = "POST"
    uri = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.monitor.name}:run"
    body = base64encode("{}")
    headers = {
      "Content-Type" = "application/json"
    }
    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_monitor_invoker]
}

resource "google_cloud_scheduler_job" "notification_delivery" {
  name        = "${local.name_prefix}-notification-delivery"
  project     = var.project_id
  region      = var.region
  schedule    = "* * * * *"
  time_zone   = "Etc/UTC"
  description = "Publishes a durable-notification delivery wake-up every minute."

  pubsub_target {
    topic_name = google_pubsub_topic.notification_delivery.id
    data       = base64encode(jsonencode({ source = "cloud-scheduler", type = "notification_delivery" }))
  }
}

resource "google_cloud_scheduler_job" "retention" {
  name        = "${local.name_prefix}-retention"
  project     = var.project_id
  region      = var.region
  schedule    = "17 3 * * *"
  time_zone   = "Etc/UTC"
  description = "Runs the repository-backed retention and deletion job."

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.api.uri}/v1/internal/retention/run"
    oidc_token { service_account_email = google_service_account.scheduler.email }
  }
}

resource "google_cloud_scheduler_job" "privacy_deletion" {
  name        = "${local.name_prefix}-privacy-deletion"
  project     = var.project_id
  region      = var.region
  schedule    = "23 3 * * *"
  time_zone   = "Etc/UTC"
  description = "Processes pending privacy deletion requests after legal-hold checks."

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.api.uri}/v1/internal/privacy/deletion-requests/process"
    oidc_token { service_account_email = google_service_account.scheduler.email }
  }
}

resource "google_monitoring_alert_policy" "cloud_run_errors" {
  display_name = "${local.name_prefix} Cloud Run errors"
  project      = var.project_id
  combiner     = "OR"
  notification_channels = var.notification_channels

  conditions {
    display_name = "5xx responses"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/request_count\" resource.type=\"cloud_run_revision\" metric.label.\"response_code_class\"=\"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "60s"
      aggregations { alignment_period = "60s" per_series_aligner = "ALIGN_RATE" }
    }
  }
}

resource "google_monitoring_alert_policy" "cloud_sql_cpu" {
  display_name = "${local.name_prefix} Cloud SQL CPU"
  project      = var.project_id
  combiner     = "OR"
  notification_channels = var.notification_channels

  conditions {
    display_name = "CPU utilization above 80 percent"
    condition_threshold {
      filter          = "metric.type=\"cloudsql.googleapis.com/database/cpu/utilization\" resource.type=\"cloudsql_database\" resource.label.\"database_id\"=\"${var.project_id}:${google_sql_database_instance.primary.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8
      duration        = "300s"
      aggregations { alignment_period = "60s" per_series_aligner = "ALIGN_MEAN" }
    }
  }
}

resource "google_logging_metric" "provider_unavailable" {
  name    = "routeshield_provider_unavailable"
  project = var.project_id
  filter  = "jsonPayload.event_type=\"provider.unavailable\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "notification_failed" {
  name    = "routeshield_notification_failed"
  project = var.project_id
  filter  = "jsonPayload.event_type=\"notification.failed\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "privacy_deletion_failed" {
  name    = "routeshield_privacy_deletion_failed"
  project = var.project_id
  filter  = "jsonPayload.event_type=\"privacy.deletion_failed\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "provider_unavailable" {
  display_name         = "${local.name_prefix} provider unavailable"
  project              = var.project_id
  combiner             = "OR"
  notification_channels = var.notification_channels

  conditions {
    display_name = "Provider returned unavailable or stale evidence"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/routeshield_provider_unavailable\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"
      aggregations { alignment_period = "60s" per_series_aligner = "ALIGN_DELTA" }
    }
  }
}

resource "google_monitoring_alert_policy" "assessment_backlog" {
  display_name         = "${local.name_prefix} assessment backlog"
  project              = var.project_id
  combiner             = "OR"
  notification_channels = var.notification_channels

  conditions {
    display_name = "Assessment-due subscription backlog above 100"
    condition_threshold {
      filter          = "metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" resource.type=\"pubsub_subscription\" resource.label.\"subscription_id\"=\"${google_pubsub_subscription.assessment_due.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 100
      duration        = "300s"
      aggregations { alignment_period = "60s" per_series_aligner = "ALIGN_MAX" }
    }
  }
}

resource "google_monitoring_alert_policy" "notification_failed" {
  display_name         = "${local.name_prefix} notification failures"
  project              = var.project_id
  combiner             = "OR"
  notification_channels = var.notification_channels

  conditions {
    display_name = "Notification retry budget exhausted"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/routeshield_notification_failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"
      aggregations { alignment_period = "60s" per_series_aligner = "ALIGN_DELTA" }
    }
  }
}

resource "google_monitoring_alert_policy" "privacy_deletion_failed" {
  display_name         = "${local.name_prefix} privacy deletion failures"
  project              = var.project_id
  combiner             = "OR"
  notification_channels = var.notification_channels

  conditions {
    display_name = "Deletion request could not be executed"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/routeshield_privacy_deletion_failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"
      aggregations { alignment_period = "60s" per_series_aligner = "ALIGN_DELTA" }
    }
  }
}

resource "google_logging_project_sink" "audit_archive" {
  name                   = "${local.name_prefix}-audit-archive"
  project                = var.project_id
  destination            = "storage.googleapis.com/${google_storage_bucket.audit_archive.name}"
  filter                 = "resource.type=(\"cloud_run_revision\" OR \"cloudsql_database\")"
  unique_writer_identity = true
}

resource "google_compute_region_network_endpoint_group" "web" {
  name                  = "${local.name_prefix}-web-neg"
  project               = var.project_id
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.web.name
  }
}

resource "google_compute_backend_service" "web" {
  name                  = "${local.name_prefix}-web-backend"
  project               = var.project_id
  protocol              = "HTTP"
  load_balancing_scheme = "EXTERNAL_MANAGED"

  backend {
    group = google_compute_region_network_endpoint_group.web.id
  }
}

resource "google_compute_url_map" "web" {
  name            = "${local.name_prefix}-web-url-map"
  project         = var.project_id
  default_service = google_compute_backend_service.web.id
}

resource "google_compute_global_address" "web" {
  count        = var.web_domain == "" ? 0 : 1
  name         = "${local.name_prefix}-web-ip"
  project      = var.project_id
  address_type = "EXTERNAL"
  ip_version   = "IPV4"
}

resource "google_compute_managed_ssl_certificate" "web" {
  count   = var.web_domain == "" ? 0 : 1
  name    = "${local.name_prefix}-web-cert"
  project = var.project_id

  managed {
    domains = [var.web_domain]
  }
}

resource "google_compute_target_https_proxy" "web" {
  count            = var.web_domain == "" ? 0 : 1
  name             = "${local.name_prefix}-web-https"
  project          = var.project_id
  url_map          = google_compute_url_map.web.id
  ssl_certificates = [google_compute_managed_ssl_certificate.web[0].id]
}

resource "google_compute_global_forwarding_rule" "web_https" {
  count                 = var.web_domain == "" ? 0 : 1
  name                  = "${local.name_prefix}-web-https"
  project               = var.project_id
  ip_address            = google_compute_global_address.web[0].id
  ip_protocol           = "TCP"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  port_range            = "443"
  target                = google_compute_target_https_proxy.web[0].id
}

resource "google_storage_bucket_iam_member" "audit_archive_writer" {
  bucket = google_storage_bucket.audit_archive.name
  role   = "roles/storage.objectCreator"
  member = google_logging_project_sink.audit_archive.writer_identity
}

output "cloud_run_uri" {
  value       = google_cloud_run_v2_service.api.uri
  description = "Authenticated RouteShield API URI."
}

output "web_cloud_run_uri" {
  value       = google_cloud_run_v2_service.web.uri
  description = "RouteShield web service URI; use the HTTPS edge URL when a domain is configured."
}

output "web_edge_ip" {
  value       = var.web_domain == "" ? null : google_compute_global_address.web[0].address
  description = "Create an A record for the approved web domain at this HTTPS load-balancer IP."
}

output "cloud_sql_connection_name" {
  value       = google_sql_database_instance.primary.connection_name
  description = "Cloud SQL connection name for the DATABASE_URL secret provisioning step."
}

output "evidence_bucket" {
  value       = google_storage_bucket.evidence.name
  description = "Bucket for retained evidence references and audit archive objects."
}

output "migration_job_name" {
  value       = google_cloud_run_v2_job.migrate.name
  description = "Cloud Run Job executed by the deployment workflow before smoke tests."
}
