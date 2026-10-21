terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.7"
    }
  }

  # Remote state. Supply the bucket at init time:
  #   terraform init -backend-config="bucket=<project>-tfstate" -backend-config="prefix=adoption-agent/<env>"
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  name   = "adoption-agent-${var.environment}"
  labels = { app = "adoption-research-agent", env = var.environment, managed-by = "terraform" }
}
