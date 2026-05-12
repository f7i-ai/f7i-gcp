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
