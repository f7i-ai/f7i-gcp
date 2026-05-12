provider "google" {
  project = var.project_id
  region  = var.region
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      ManagedBy  = "terraform"
      Environment = var.environment
      Repository  = "f7i-ai/f7i-gcp"
    }
  }
}
