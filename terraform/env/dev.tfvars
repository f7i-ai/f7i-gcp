# Branch dev — paired with gcp-dev.backend.hcl
project_id  = "anomaly-detection-dev-496103"
region      = "australia-southeast1"
environment = "dev"

# After first apply, get the SA numeric ID and uncomment:
# gcp_bridge_sa_id = "123456789012345678901"
# Run: gcloud iam service-accounts describe \
#        aws-bridge-fn-dev@anomaly-detection-dev-496103.iam.gserviceaccount.com \
#        --format='value(uniqueId)'
