# ---------------------------------------------------------------------------
# Operations: alert when a research run fails
# ---------------------------------------------------------------------------
resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  display_name = "Adoption agent alerts (${var.environment})"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

resource "google_monitoring_alert_policy" "job_failed" {
  display_name = "${local.name}: research run failed"
  combiner     = "OR"

  conditions {
    display_name = "Cloud Run Job execution failed"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_job\" AND resource.labels.job_name = \"${google_cloud_run_v2_job.agent.name}\" AND metric.type = \"run.googleapis.com/job/completed_execution_count\" AND metric.labels.result = \"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = [for c in google_monitoring_notification_channel.email : c.id]
  documentation {
    content   = "A scheduled research run failed. Check Cloud Logging for job ${google_cloud_run_v2_job.agent.name}; evidence gathered before the failure is persisted in gs://${google_storage_bucket.state.name}. Re-run with `--resume`."
    mime_type = "text/markdown"
  }
}
