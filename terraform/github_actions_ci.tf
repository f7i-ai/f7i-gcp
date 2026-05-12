# GitHub Actions OIDC → AWS — IAM role used by CI (configure-aws-credentials).
# Managed only from the dev Terraform state so prod state does not fight the same AWS resource.

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_actions_assume" {
  statement {
    sid     = "GitHubOIDC"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "ForAnyValue:StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:f7i-ai/f7i-gcp:ref:refs/heads/dev",
        "repo:f7i-ai/f7i-gcp:ref:refs/heads/main",
        "repo:f7i-ai/f7i-gcp:pull_request",
        "repo:f7i-ai/f7i-gcp:environment:terraform-apply-dev",
        "repo:f7i-ai/f7i-gcp:environment:terraform-apply-prod",
      ]
    }
  }
}

resource "aws_iam_role" "github_terraform" {
  count = var.environment == "dev" ? 1 : 0

  name               = "f7i-gcp-github-terraform"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume.json

  lifecycle {
    ignore_changes = [
      managed_policy_arns,
      tags,
      tags_all,
    ]
  }
}
