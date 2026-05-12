# Branch dev — paired with gcp-dev.backend.hcl
project_id  = "anomaly-detection-dev-496103"
region      = "australia-southeast1"
environment = "dev"

# Populated from aws/terraform outputs after first AWS apply
aws_bridge_role_arn        = "arn:aws:iam::432534569171:role/gcp-aws-bridge-dev"
aws_bridge_s3_bucket       = "f7i-gcp-bridge-dev-432534569171"
aws_bridge_eventbridge_bus = "default"
