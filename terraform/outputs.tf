output "project_id" {
  description = "Target GCP project ID."
  value       = var.project_id
}

output "region" {
  description = "Configured default GCP region."
  value       = var.region
}

output "function_url" {
  description = "Invocation URL for the aws-bridge Cloud Function."
  value       = google_cloudfunctions2_function.aws_bridge.service_config[0].uri
}

output "function_sa_email" {
  description = "Service account email — use with gcloud to get the numeric unique ID for gcp_bridge_sa_id."
  value       = google_service_account.aws_bridge_fn.email
}

output "aws_account_id" {
  description = "AWS account ID the provider is authenticated to."
  value       = data.aws_caller_identity.current.account_id
}

output "gcp_bridge_role_arn" {
  description = "ARN of the IAM role assumed by the aws-bridge function."
  value       = aws_iam_role.gcp_aws_bridge.arn
}

output "bridge_s3_bucket" {
  description = "S3 bucket name for GCP function uploads."
  value       = aws_s3_bucket.bridge.id
}

output "bridge_eventbridge_bus" {
  description = "EventBridge bus name."
  value       = aws_cloudwatch_event_bus.bridge.name
}

output "github_terraform_role_arn" {
  description = "GitHub Actions OIDC role for Terraform CI (managed when environment=dev)."
  value       = try(aws_iam_role.github_terraform[0].arn, null)
}

output "public_invoker_hint" {
  description = "Shell snippet to allow unauthenticated HTTPS calls when manage_cloud_function_public_invoker is false."
  value       = var.manage_cloud_function_public_invoker ? null : "gcloud functions add-invoker-policy-binding ${google_cloudfunctions2_function.aws_bridge.name} --project=${var.project_id} --region=${var.region} --member=allUsers"
}

output "vertex_trainer_ci_grant_hint" {
  description = "One-time grants the project owner must run for the CI deployer SA so it can manage the vertex-trainer stack (Workload Identity Pool + project IAM + SA self-binding)."
  value       = <<-EOT
    # Run once as a project owner. Replace CI_SA with your terraform CI service account.
    CI_SA=terraform-github-ci@anomaly-detection-496003.iam.gserviceaccount.com
    for role in roles/iam.workloadIdentityPoolAdmin \
                roles/resourcemanager.projectIamAdmin \
                roles/iam.serviceAccountAdmin; do
      gcloud projects add-iam-policy-binding ${var.project_id} \
        --member=serviceAccount:$CI_SA --role=$role
    done
  EOT
}

output "vertex_trainer_lambda_name" {
  description = "AWS Lambda function name for the vertex-trainer."
  value       = aws_lambda_function.vertex_trainer.function_name
}

output "vertex_trainer_lambda_role_arn" {
  description = "Execution role ARN of the vertex-trainer Lambda (federated principal in the GCP WIF pool)."
  value       = aws_iam_role.vertex_trainer_lambda.arn
}

output "vertex_trainer_sa_email" {
  description = "GCP service account the Lambda impersonates and Vertex CustomJobs run as."
  value       = google_service_account.vertex_trainer.email
}

output "vertex_trainer_staging_bucket" {
  description = "GCS bucket holding training inputs and Vertex CustomJob outputs."
  value       = google_storage_bucket.vertex_trainer_staging.name
}

output "service_account_token_creator_hint" {
  description = "Run once as project owner: function SA must grant Token Creator to itself for generateIdToken→AWS (CI usually cannot setIamPolicy on this SA)."
  value       = "gcloud iam service-accounts add-iam-policy-binding ${google_service_account.aws_bridge_fn.email} --project=${var.project_id} --member=serviceAccount:${google_service_account.aws_bridge_fn.email} --role=roles/iam.serviceAccountTokenCreator"
}
