# APIs required by the agent runtime, CI/CD and observability.
resource "google_project_service" "services" {
  for_each = toset([
    "aiplatform.googleapis.com",       # Gemini on Vertex AI
    "run.googleapis.com",              # Cloud Run Jobs
    "artifactregistry.googleapis.com", # container images
    "cloudbuild.googleapis.com",       # Cloud Build pipelines
    "cloudscheduler.googleapis.com",   # scheduled refresh
    "iam.googleapis.com",
    "iamcredentials.googleapis.com", # WIF token exchange
    "sts.googleapis.com",            # WIF security token service
    "cloudtrace.googleapis.com",     # OpenTelemetry traces
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "dlp.googleapis.com",     # PII de-identification
    "storage.googleapis.com", # long-term memory bucket
  ])
  service            = each.value
  disable_on_destroy = false
}
