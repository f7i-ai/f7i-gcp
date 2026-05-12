variable "project_id" {
  description = "GCP project ID where resources are managed."
  type        = string
}

variable "region" {
  description = "Default region for regional resources."
  type        = string
  default     = "australia-southeast1"
}

variable "environment" {
  description = "Logical environment: dev or prod."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be \"dev\" or \"prod\"."
  }
}

variable "aws_region" {
  description = "AWS region the bridge function targets."
  type        = string
  default     = "ap-southeast-2"
}

variable "aws_bridge_role_arn" {
  description = "ARN of the AWS IAM role the function assumes via OIDC (output from aws/terraform)."
  type        = string
}

variable "aws_bridge_s3_bucket" {
  description = "Name of the S3 bucket the function uploads to (output from aws/terraform)."
  type        = string
}

variable "aws_bridge_eventbridge_bus" {
  description = "Name of the EventBridge bus the function publishes to (output from aws/terraform)."
  type        = string
  default     = "default"
}

