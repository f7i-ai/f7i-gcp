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

variable "gcp_vertex_completion_sa_id" {
  description = "Numeric unique ID of the GCP Service Account for the vertex-completion-bridge function. Used in the AWS OIDC trust condition (azp -> aud). Leave empty on first apply — fill in after apply creates the SA, then re-apply."
  type        = string
  default     = ""
}

variable "ci_deployer_sa_email" {
  description = "GCP service account used by GitHub Actions to apply this Terraform. Granted roles/owner on var.project_id below — the GCP analog of the AWS AdministratorAccess attachment on the github_terraform role."
  type        = string
  default     = "terraform-github-ci@anomaly-detection-496003.iam.gserviceaccount.com"
}

variable "vertex_trainer_image" {
  description = "Container image URI for Vertex AI CustomJobs. Defaults to Vertex's prebuilt PyTorch CPU image — already has torch, numpy, pandas, sklearn, and google-cloud-storage. The Lambda's CustomJob spec downloads our entrypoint + training code from GCS at job start, so no Docker build is needed."
  type        = string
  default     = "us-docker.pkg.dev/vertex-ai/training/pytorch-cpu.2-3.py310:latest"
}

variable "vertex_machine_type" {
  description = "Vertex AI worker machine type for the training CustomJob (e.g. n1-standard-4, n1-highmem-8)."
  type        = string
  default     = "n1-standard-4"
}

variable "manage_cloud_function_public_invoker" {
  description = "If true, grants allUsers cloudfunctions.invoker on the Gen2 HTTP function. Requires the Terraform/deployer identity to have permission cloudfunctions.functions.setIamPolicy (e.g. roles/cloudfunctions.admin). Leave false if CI uses a narrower SA and add the binding once via gcloud/Console."
  type        = bool
  default     = false
}
