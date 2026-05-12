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
  description = "Logical environment: dev (branch dev) or prod (branch main). Used for tagging and CI wiring."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be \"dev\" or \"prod\"."
  }
}
