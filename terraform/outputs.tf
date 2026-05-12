output "project_id" {
  description = "Target GCP project ID."
  value       = var.project_id
}

output "region" {
  description = "Configured default region."
  value       = var.region
}

output "function_url" {
  description = "Invocation URL for the aws-bridge Cloud Function."
  value       = google_cloudfunctions2_function.aws_bridge.service_config[0].uri
}

output "function_sa_email" {
  description = "Service account email used by the aws-bridge function."
  value       = google_service_account.aws_bridge_fn.email
}
