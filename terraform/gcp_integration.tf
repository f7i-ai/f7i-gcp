# Google OIDC provider so AWS trusts tokens issued by accounts.google.com.
# One per AWS account — shared across all GCP SA integrations.

resource "aws_iam_openid_connect_provider" "google" {
  url = "https://accounts.google.com"
  # JWT `aud` must appear here. Google may return either form for the STS audience.
  client_id_list = local.bridge_google_aud_values

  # Google's stable root CA thumbprint
  thumbprint_list = ["08745487e891c19e3078c1f2a07e452950ef36f6"]
}

locals {
  # JWT `aud` must appear on the IAM OIDC provider. Google may return either form for the STS audience.
  bridge_google_aud_values = [
    "sts.amazonaws.com",
    "https://sts.amazonaws.com",
  ]
}

data "aws_iam_policy_document" "gcp_aws_bridge_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.google.arn]
    }

    # AWS requires this condition for any Google OIDC trust policy.
    # The function mints the token with audience "sts.amazonaws.com".
    condition {
      test     = "StringEquals"
      variable = "accounts.google.com:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

# IAM role assumed by the GCP Cloud Function SA
resource "aws_iam_role" "gcp_aws_bridge" {
  name        = "gcp-aws-bridge-${var.environment}"
  description = "Assumed by GCP SA aws-bridge-fn-${var.environment} via Google OIDC."

  assume_role_policy = data.aws_iam_policy_document.gcp_aws_bridge_assume.json

  tags = {
    ManagedBy   = "terraform"
    Environment = var.environment
    Repository  = "f7i-ai/f7i-gcp"
  }
}

resource "aws_iam_role_policy" "gcp_aws_bridge" {
  name = "gcp-aws-bridge-permissions"
  role = aws_iam_role.gcp_aws_bridge.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3Upload"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:ListBucket",
        ]
        Resource = [
          aws_s3_bucket.bridge.arn,
          "${aws_s3_bucket.bridge.arn}/*",
        ]
      },
      {
        Sid      = "EventBridgePublish"
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = aws_cloudwatch_event_bus.bridge.arn
      }
    ]
  })
}

# S3 bucket the Cloud Function uploads into
resource "aws_s3_bucket" "bridge" {
  bucket = "f7i-gcp-bridge-${var.environment}-${data.aws_caller_identity.current.account_id}"

  tags = {
    ManagedBy   = "terraform"
    Environment = var.environment
    Repository  = "f7i-ai/f7i-gcp"
  }
}

resource "aws_s3_bucket_ownership_controls" "bridge" {
  bucket = aws_s3_bucket.bridge.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_public_access_block" "bridge" {
  bucket                  = aws_s3_bucket.bridge.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# S3 → EventBridge notifications (objects uploaded automatically fire events)
resource "aws_s3_bucket_notification" "bridge" {
  bucket      = aws_s3_bucket.bridge.id
  eventbridge = true
}

# EventBridge bus
resource "aws_cloudwatch_event_bus" "bridge" {
  name = "f7i-gcp-bridge-${var.environment}"

  tags = {
    ManagedBy   = "terraform"
    Environment = var.environment
    Repository  = "f7i-ai/f7i-gcp"
  }
}
