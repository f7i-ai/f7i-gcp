locals {
  common_labels = {
    managed_by  = "terraform"
    environment = var.environment
    repository  = "f7i-ai/f7i-gcp"
  }
}

resource "google_project_service" "core_apis" {
  for_each = toset([
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
    "cloudfunctions.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
  ])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# ── Service Account ────────────────────────────────────────────────────────────

resource "google_service_account" "aws_bridge_fn" {
  project      = var.project_id
  account_id   = "aws-bridge-fn-${var.environment}"
  display_name = "AWS Bridge Cloud Function (${var.environment})"
  description  = "Runs the aws-bridge Cloud Function; assumes AWS role via OIDC to write S3 + EventBridge."
}

# ── Source bucket (Cloud Functions Gen2 uploads zip here) ─────────────────────

resource "google_storage_bucket" "fn_source" {
  project                     = var.project_id
  name                        = "${var.project_id}-fn-source-${var.environment}"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  labels = local.common_labels
}

resource "google_storage_bucket_object" "aws_bridge_source" {
  name   = "aws-bridge/${filemd5("${path.module}/../functions/aws-bridge/main.py")}.zip"
  bucket = google_storage_bucket.fn_source.name
  source = data.archive_file.aws_bridge_zip.output_path
}

data "archive_file" "aws_bridge_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../functions/aws-bridge"
  output_path = "/tmp/aws-bridge.zip"
}

# ── Cloud Function (Gen 2) ─────────────────────────────────────────────────────

resource "google_cloudfunctions2_function" "aws_bridge" {
  project  = var.project_id
  name     = "aws-bridge-${var.environment}"
  location = var.region

  description = "Test function: uploads a file to S3 and publishes to EventBridge via OIDC."

  labels = local.common_labels

  build_config {
    runtime     = "python312"
    entry_point = "handle"
    source {
      storage_source {
        bucket = google_storage_bucket.fn_source.name
        object = google_storage_bucket_object.aws_bridge_source.name
      }
    }
  }

  service_config {
    service_account_email          = google_service_account.aws_bridge_fn.email
    max_instance_count             = 3
    min_instance_count             = 0
    available_memory               = "256M"
    timeout_seconds                = 60
    all_traffic_on_latest_revision = true

    environment_variables = {
      AWS_ROLE_ARN        = var.aws_bridge_role_arn
      AWS_S3_BUCKET       = var.aws_bridge_s3_bucket
      AWS_EVENTBRIDGE_BUS = var.aws_bridge_eventbridge_bus
      AWS_REGION          = var.aws_region
    }
  }

  depends_on = [google_project_service.core_apis]
}

# Allow unauthenticated HTTP invocations (test only — lock down for prod)
resource "google_cloud_run_v2_service_iam_member" "aws_bridge_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloudfunctions2_function.aws_bridge.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
