# ---------------------------------------------------------------------------
# Least-privilege service accounts
# ---------------------------------------------------------------------------

# Identity the agent runs as (Cloud Run Job).
resource "google_service_account" "runtime" {
  account_id   = "${local.name}-run"
  display_name = "Adoption research agent runtime (${var.environment})"
}

resource "google_project_iam_member" "runtime_roles" {
  for_each = toset([
    "roles/aiplatform.user",         # call Gemini
    "roles/cloudtrace.agent",        # export OpenTelemetry spans
    "roles/logging.logWriter",       # structured logs
    "roles/monitoring.metricWriter", # metrics
    "roles/dlp.user",                # PII de-identification
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# Identity GitHub Actions / Cloud Build deploy as.
resource "google_service_account" "deployer" {
  account_id   = "${local.name}-cicd"
  display_name = "Adoption research agent CI/CD deployer (${var.environment})"
}

resource "google_project_iam_member" "deployer_roles" {
  for_each = toset([
    "roles/run.developer",            # update the Cloud Run Job
    "roles/artifactregistry.writer",  # push images
    "roles/aiplatform.user",          # scheduled refresh workflow calls Gemini
    "roles/cloudbuild.builds.editor", # trigger Cloud Build
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# The deployer may deploy jobs that run as the runtime identity (and nothing else).
resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# Identity Cloud Scheduler uses to start the job.
resource "google_service_account" "scheduler" {
  account_id   = "${local.name}-sched"
  display_name = "Adoption research agent scheduler (${var.environment})"
}
