locals {
  common_labels = {
    managed_by  = "terraform"
    environment = var.environment
    repository  = "f7i-gcp"
  }
}

resource "google_project_service" "core_apis" {
  for_each = toset([
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
  ])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}
