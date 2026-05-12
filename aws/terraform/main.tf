data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Lightweight resource so a dev merge produces a real apply (not only data sources).
resource "terraform_data" "ci_bootstrap" {
  input = {
    environment = var.environment
    account_key   = var.account_key
    purpose       = "f7i-gcp-aws-bootstrap"
  }
  
}
