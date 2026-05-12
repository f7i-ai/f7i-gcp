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
