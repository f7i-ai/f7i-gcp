# vertex-trainer Lambda + GCP Workload Identity Federation
# ---------------------------------------------------------------------------
# AWS Lambda (in this account) calls into GCP (Vertex AI + GCS) using an
# AWS-typed Workload Identity Pool — no service-account JSON key on AWS.
# The Lambda's execution role is the federated principal; impersonating the
# vertex-trainer SA gives it permission to submit Vertex CustomJobs and write
# to the staging bucket.

data "google_project" "current" {
  project_id = var.project_id
}

# ── GCP: Workload Identity Pool (AWS as provider) ────────────────────────────

resource "google_iam_workload_identity_pool" "aws_pool" {
  project                   = var.project_id
  workload_identity_pool_id = "aws-lambda-${var.environment}"
  display_name              = "AWS Lambda Pool (${var.environment})"
  description               = "Federates AWS Lambda execution roles into GCP service accounts."
}

resource "google_iam_workload_identity_pool_provider" "aws_provider" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.aws_pool.workload_identity_pool_id
  workload_identity_pool_provider_id = "aws-${var.environment}"
  display_name                       = "AWS account ${data.aws_caller_identity.current.account_id}"

  aws {
    account_id = data.aws_caller_identity.current.account_id
  }
}

# ── GCP: Service Account the Lambda impersonates ─────────────────────────────

resource "google_service_account" "vertex_trainer" {
  project      = var.project_id
  account_id   = "vertex-trainer-${var.environment}"
  display_name = "Vertex Trainer (impersonated by AWS Lambda)"
  description  = "Submits Vertex AI CustomJobs and writes to the training staging bucket."
}

# AWS Lambda execution role → impersonate the GCP SA via the AWS-typed pool.
resource "google_service_account_iam_member" "vertex_trainer_wif" {
  service_account_id = google_service_account.vertex_trainer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.aws_pool.name}/attribute.aws_role/arn:aws:sts::${data.aws_caller_identity.current.account_id}:assumed-role/${aws_iam_role.vertex_trainer_lambda.name}"
}

# Vertex AI submission + GCS staging permissions on the SA.
resource "google_project_iam_member" "vertex_trainer_aiplatform_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.vertex_trainer.email}"
}

# CustomJob runs *as* this same SA, so it needs serviceAccountUser on itself.
resource "google_service_account_iam_member" "vertex_trainer_self_user" {
  service_account_id = google_service_account.vertex_trainer.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.vertex_trainer.email}"
}

# ── GCP: staging bucket for training inputs + Vertex job outputs ─────────────

resource "google_storage_bucket" "vertex_trainer_staging" {
  project                     = var.project_id
  name                        = "${var.project_id}-vertex-trainer-${var.environment}"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = var.environment == "dev"

  labels = local.common_labels
}

resource "google_storage_bucket_iam_member" "vertex_trainer_bucket_admin" {
  bucket = google_storage_bucket.vertex_trainer_staging.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.vertex_trainer.email}"
}

# ── AWS: Lambda execution role ───────────────────────────────────────────────

data "aws_iam_policy_document" "vertex_trainer_lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "vertex_trainer_lambda" {
  name               = "vertex-trainer-lambda-${var.environment}"
  description        = "Execution role for the vertex-trainer Lambda (federated into GCP via WIF)."
  assume_role_policy = data.aws_iam_policy_document.vertex_trainer_lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "vertex_trainer_lambda_basic" {
  role       = aws_iam_role.vertex_trainer_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Prototype: no AWS data-source permissions yet. The Lambda only needs
# basic execution (logs) + the GCP federation to upload a mock CSV and
# submit a Vertex job. DynamoDB/SSM/Bedrock grants come back when we
# wire up the real data path.

# ── Lambda deployment package ────────────────────────────────────────────────

locals {
  vertex_trainer_src     = "${path.module}/../lambdas/vertex-trainer"
  vertex_trainer_build   = "${local.vertex_trainer_src}/build"
  vertex_trainer_zip     = "/tmp/vertex-trainer-${var.environment}.zip"
  vertex_trainer_sources = fileset(local.vertex_trainer_src, "*.py")
  vertex_trainer_hash = sha1(join(",", concat(
    [filemd5("${local.vertex_trainer_src}/requirements.txt")],
    [for f in local.vertex_trainer_sources : filemd5("${local.vertex_trainer_src}/${f}")],
  )))
}

# Build the deployment package (pip install + sources + zip) in one shot.
# Both the build directory and the zip are produced at apply time, so the
# Lambda resource references the zip path directly — no archive_file data
# source (which would be evaluated at plan time, before the dir exists).
resource "null_resource" "vertex_trainer_build" {
  triggers = {
    content_hash = local.vertex_trainer_hash
  }

  provisioner "local-exec" {
    # POSIX sh — CI uses dash, which rejects `set -o pipefail`. Plain `set -eu` is enough.
    command = <<-EOT
      set -eu
      rm -rf ${local.vertex_trainer_build} ${local.vertex_trainer_zip}
      mkdir -p ${local.vertex_trainer_build}
      python3 -m pip install --quiet --target ${local.vertex_trainer_build} \
        --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 \
        --only-binary=:all: --upgrade -r ${local.vertex_trainer_src}/requirements.txt
      cp ${local.vertex_trainer_src}/*.py ${local.vertex_trainer_build}/
      cd ${local.vertex_trainer_build} && zip -qr ${local.vertex_trainer_zip} .
    EOT
  }
}

# ── AWS: Lambda function ─────────────────────────────────────────────────────

resource "aws_lambda_function" "vertex_trainer" {
  function_name    = "vertex-trainer-${var.environment}"
  description      = "Gathers sensor data from DynamoDB and submits a Vertex AI training job."
  role             = aws_iam_role.vertex_trainer_lambda.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = local.vertex_trainer_zip
  source_code_hash = local.vertex_trainer_hash
  timeout          = 900
  memory_size      = 2048

  depends_on = [null_resource.vertex_trainer_build]

  environment {
    variables = {
      DEPLOYMENT_ENV       = var.environment
      GCP_PROJECT_ID       = var.project_id
      VERTEX_LOCATION      = var.region
      GCS_STAGING_BUCKET   = google_storage_bucket.vertex_trainer_staging.name
      VERTEX_TRAINER_SA    = google_service_account.vertex_trainer.email
      VERTEX_TRAINER_IMAGE = var.vertex_trainer_image
      VERTEX_MACHINE_TYPE  = var.vertex_machine_type
      GCP_WIF_CONFIG_JSON = jsonencode({
        type                              = "external_account"
        audience                          = "//iam.googleapis.com/projects/${data.google_project.current.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.aws_pool.workload_identity_pool_id}/providers/${google_iam_workload_identity_pool_provider.aws_provider.workload_identity_pool_provider_id}"
        subject_token_type                = "urn:ietf:params:aws:token-type:aws4_request"
        service_account_impersonation_url = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/${google_service_account.vertex_trainer.email}:generateAccessToken"
        token_url                         = "https://sts.googleapis.com/v1/token"
        credential_source = {
          environment_id                 = "aws1"
          regional_cred_verification_url = "https://sts.{region}.amazonaws.com?Action=GetCallerIdentity&Version=2011-06-15"
        }
      })
    }
  }
}
