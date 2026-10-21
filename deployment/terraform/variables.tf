variable "project_id" {
  description = "Google Cloud project that hosts the agent."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Artifact Registry and Cloud Scheduler."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Deployment environment name (e.g. staging, prod)."
  type        = string
  default     = "staging"
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of dev, staging, prod."
  }
}

variable "github_repository" {
  description = "GitHub repository allowed to deploy via Workload Identity Federation (owner/name)."
  type        = string
  default     = "johnenoch1966/John-Enoch"
}

variable "image" {
  description = "Container image for the Cloud Run Job. CI/CD updates this on every release."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/job:latest"
}

variable "model" {
  description = "Gemini model used by the agents."
  type        = string
  default     = "gemini-3.8-flash"
}

variable "schedule" {
  description = "Cron schedule (UTC) for the automated research refresh. Empty string disables it."
  type        = string
  default     = "0 6 * * 1"
}

variable "alert_email" {
  description = "Optional email address notified when a research run fails."
  type        = string
  default     = ""
}

variable "pii_backend" {
  description = "PII masking backend: regex (offline) or dlp (Cloud Sensitive Data Protection)."
  type        = string
  default     = "dlp"
  validation {
    condition     = contains(["regex", "dlp"], var.pii_backend)
    error_message = "pii_backend must be regex or dlp."
  }
}
