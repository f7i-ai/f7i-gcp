# Branch dev — paired with gcp-dev.backend.hcl
project_id  = "anomaly-detection-dev-496103"
region      = "australia-southeast1"
environment = "dev"

# Terraform CI SA needs roles/cloudfunctions.admin (or setIamPolicy) to manage this in apply.
# If false, run once as a project owner: see output public_invoker_hint after apply.
manage_cloud_function_public_invoker = false

gcp_bridge_sa_id = "104252570082934414326"

# Vertex AI training image (Artifact Registry). Empty until the training
# container is published — the Lambda will deploy successfully but invoking
# it will fail until this is set, since it's required when submitting the job.
vertex_trainer_image = ""
