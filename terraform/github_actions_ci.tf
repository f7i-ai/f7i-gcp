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
      tags,
      tags_all,
    ]
  }
}

# Prefer attachment resource over deprecated aws_iam_role.managed_policy_arns.
resource "aws_iam_role_policy_attachment" "github_terraform_admin" {
  count = var.environment == "dev" ? 1 : 0

  role       = aws_iam_role.github_terraform[0].name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# GCP analog: grant the CI deployer SA roles/owner on this project. Editor
# (the default) can't manage IAM policies, which the vertex-trainer stack
# needs (Workload Identity Pool, project IAM binding, SA self-binding).
# Bootstrap once: a project owner must run --
#   gcloud projects add-iam-policy-binding ${PROJECT_ID} \
#     --member=serviceAccount:${ci_deployer_sa_email} --role=roles/owner
# After that, this binding is Terraform-managed and CI can apply.
resource "google_project_iam_member" "github_terraform_owner" {
  project = var.project_id
  role    = "roles/owner"
  member  = "serviceAccount:${var.ci_deployer_sa_email}"
}
