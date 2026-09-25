# Two roles, both least-privilege:
# * app runtime: read its own secrets, read/write evidence objects, write logs.
#   Trusted by EKS Pod Identity and ECS tasks (Phases 23-24).
# * CI: GitHub Actions (OIDC, no stored AWS keys) may push images to the two ECR
#   repositories, only from this repository's main branch and version tags.

data "aws_iam_policy_document" "app_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com", "ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "${local.name}-app"
  assume_role_policy = data.aws_iam_policy_document.app_trust.json
}

data "aws_iam_policy_document" "app" {
  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = concat([for s in aws_secretsmanager_secret.app : s.arn], [aws_db_instance.main.master_user_secret[0].secret_arn])
  }
  statement {
    sid       = "EvidenceObjects"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.evidence.arn}/*"]
  }
  statement {
    sid       = "EvidenceList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.evidence.arn]
  }
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [for g in aws_cloudwatch_log_group.app : "${g.arn}:*"]
  }
}

resource "aws_iam_role_policy" "app" {
  name   = "app"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app.json
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "ci_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repository}:ref:refs/heads/main",
        "repo:${var.github_repository}:ref:refs/tags/v*",
      ]
    }
  }
}

resource "aws_iam_role" "ci" {
  name               = "${local.name}-ci-ecr-push"
  assume_role_policy = data.aws_iam_policy_document.ci_trust.json
}

data "aws_iam_policy_document" "ci" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "PushOwnRepositories"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [for r in aws_ecr_repository.app : r.arn]
  }
}

resource "aws_iam_role_policy" "ci" {
  name   = "ecr-push"
  role   = aws_iam_role.ci.id
  policy = data.aws_iam_policy_document.ci.json
}
