provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(
      {
        ManagedBy   = "terraform"
        Environment = var.environment
        Repository  = "f7i-gcp"
      },
      var.account_key != "" ? { AccountKey = var.account_key } : {}
    )
  }
}
