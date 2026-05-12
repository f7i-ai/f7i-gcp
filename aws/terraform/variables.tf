variable "aws_region" {
  description = "AWS region for provider and regional data sources."
  type        = string
  default     = "ap-southeast-2"
}

variable "environment" {
  description = "dev (branch dev) or prod (branch main); used for tagging and CI."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be \"dev\" or \"prod\"."
  }
}

variable "account_key" {
  description = "Stable slug for this prod AWS account (aws/config/prod-accounts.json). Empty for dev."
  type        = string
  default     = ""
}

variable "gcp_bridge_sa_id" {
  description = "Numeric unique ID of the GCP Service Account for the aws-bridge function. Used in the OIDC trust condition. Leave empty on first apply — fill in after GCP apply creates the SA."
  type        = string
  default     = ""
}
