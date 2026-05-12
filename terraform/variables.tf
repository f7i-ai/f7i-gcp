variable "project_id" {
  description = "GCP project ID where resources are managed."
  type        = string
}

variable "region" {
  description = "Default GCP region for regional resources."
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
  description = "AWS region for the bridge resources and provider."
  type        = string
  default     = "ap-southeast-2"
}

variable "gcp_bridge_sa_id" {
  description = "Numeric unique ID of the GCP Service Account for the aws-bridge function. Used in the AWS OIDC trust condition. Leave empty on first apply — fill in after apply creates the SA."
  type        = string
  default     = ""
}
