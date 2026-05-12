terraform {
  required_version = ">= 1.5.0"

  # Bucket and prefix are supplied at init time (local: backend.hcl; CI: -backend-config).
  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}
