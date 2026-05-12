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
  description = "Stable slug for this prod AWS account (from aws/config/prod-accounts.json). Empty for dev."
  type        = string
  default     = ""
}
