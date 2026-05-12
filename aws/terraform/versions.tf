terraform {
  required_version = ">= 1.5.0"

  # Remote state in Google Cloud Storage (same bucket pattern as GCP stacks; prefix isolates AWS).
  # Never run `terraform init` without -backend-config; do not use a local backend.
  backend "gcs" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
