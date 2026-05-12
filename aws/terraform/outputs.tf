output "aws_account_id" {
  description = "AWS account ID from the active credential."
  value       = data.aws_caller_identity.current.account_id
}

output "aws_region" {
  description = "AWS region from provider configuration."
  value       = data.aws_region.current.name
}

output "environment" {
  description = "Logical environment name."
  value       = var.environment
}

output "account_key" {
  description = "Prod account slug when deploying production; empty in dev."
  value       = var.account_key
}

output "gcp_bridge_role_arn" {
  description = "ARN of the IAM role assumed by the GCP aws-bridge function. Set as aws_bridge_role_arn in terraform/env/dev.tfvars."
  value       = aws_iam_role.gcp_aws_bridge.arn
}

output "bridge_s3_bucket" {
  description = "S3 bucket name for GCP function uploads. Set as aws_bridge_s3_bucket in terraform/env/dev.tfvars."
  value       = aws_s3_bucket.bridge.id
}

output "bridge_eventbridge_bus" {
  description = "EventBridge bus name. Set as aws_bridge_eventbridge_bus in terraform/env/dev.tfvars."
  value       = aws_cloudwatch_event_bus.bridge.name
}
