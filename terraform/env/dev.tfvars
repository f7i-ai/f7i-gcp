# Branch dev — paired with gcp-dev.backend.hcl
project_id  = "anomaly-detection-dev-496103"
region      = "australia-southeast1"
environment = "dev"

# Terraform CI SA needs roles/cloudfunctions.admin (or setIamPolicy) to manage this in apply.
# If false, run once as a project owner: see output public_invoker_hint after apply.
manage_cloud_function_public_invoker = false

# After first apply, get the SA numeric ID and uncomment:
# gcp_bridge_sa_id = "123456789012345678901"
# Run: gcloud iam service-accounts describe \
#        aws-bridge-fn-dev@anomaly-detection-dev-496103.iam.gserviceaccount.com \
#        --format='value(uniqueId)'
