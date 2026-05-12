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

variable "manage_cloud_function_public_invoker" {
  description = "If true, grants allUsers cloudfunctions.invoker on the Gen2 HTTP function. Requires the Terraform/deployer identity to have permission cloudfunctions.functions.setIamPolicy (e.g. roles/cloudfunctions.admin). Leave false if CI uses a narrower SA and add the binding once via gcloud/Console."
  type        = bool
  default     = false
}

variable "manage_aws_bridge_sa_token_creator_self" {
  description = "If true, grants the aws-bridge function SA roles/iam.serviceAccountTokenCreator on itself (required for generateIdToken toward AWS). Needs deployer iam.serviceAccounts.setIamPolicy on that SA (e.g. roles/iam.serviceAccountAdmin). If false, run output service_account_token_creator_hint once as project owner."
  type        = bool
  default     = false
}
