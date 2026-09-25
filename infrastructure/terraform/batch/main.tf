# ECS/Fargate batch workload (Phase 24): the embedding backfill as a one-shot task.
#
# It runs `python -m app.workers.embeddings --drain N` with the API image: embed every
# pending fact version, then exit (exit code 1 if any job failed permanently). It needs
# only PostgreSQL and the embedding provider, so it runs with process_role=batch.
#
# Cost: Fargate bills per second only while a task runs; the cluster itself is free.
# The schedule is created disabled. Nothing runs until you `make batch-run` or enable it.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = var.owner
      Stack       = "batch"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "contextledger"
}

variable "environment" {
  type    = string
  default = "demo"
}

variable "owner" {
  type = string
}

variable "state_bucket" {
  type        = string
  description = "The bootstrap state bucket (same as in backend.hcl)."
}

variable "image_tag" {
  type        = string
  description = "Tag of the contextledger-api image in ECR (e.g. the git short SHA)."
}

variable "max_batches" {
  type        = number
  default     = 200
  description = "Upper bound on batches per run (each batch is CONTEXTLEDGER_EMBEDDING_BATCH_SIZE jobs)."
}

variable "schedule_expression" {
  type    = string
  default = "cron(0 6 * * ? *)"
}

variable "schedule_enabled" {
  type        = bool
  default     = false
  description = "Off by default: enable only while the demo environment exists."
}

variable "log_retention_days" {
  type    = number
  default = 14
}

data "terraform_remote_state" "core" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "core/terraform.tfstate"
    region = var.region
  }
}

data "aws_caller_identity" "current" {}

locals {
  name = "${var.project}-${var.environment}"
  core = data.terraform_remote_state.core.outputs
  # With the NAT gateway the task stays in private subnets. Without it (the cheap
  # default) it runs in public subnets with a public IP for outbound calls only:
  # its security group has no ingress rules at all.
  private       = local.core.nat_gateway_enabled
  subnet_ids    = local.private ? local.core.private_subnet_ids : local.core.public_subnet_ids
  db_secret_arn = local.core.db_master_secret_arn
  openai_arn    = local.core.app_secret_arns["openai-api-key"]
}

# --- network ----------------------------------------------------------------------------

resource "aws_security_group" "task" {
  name        = "${local.name}-batch"
  description = "Embedding backfill task: egress only"
  vpc_id      = local.core.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.task.id
  description       = "ECR, Secrets Manager, CloudWatch Logs, embedding provider"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "postgres" {
  security_group_id            = aws_security_group.task.id
  description                  = "RDS PostgreSQL"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = local.core.db_security_group_id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_batch" {
  security_group_id            = local.core.db_security_group_id
  description                  = "PostgreSQL from the ECS batch task"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.task.id
}

# --- IAM ----------------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# Execution role: used by the ECS agent to pull the image, write logs and resolve the
# task's secrets. Only the two secrets this task needs.
resource "aws_iam_role" "execution" {
  name               = "${local.name}-batch-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.db_secret_arn, local.openai_arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-task-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# --- task -----------------------------------------------------------------------------------

resource "aws_ecs_cluster" "batch" {
  name = "${local.name}-batch"
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_cloudwatch_log_group" "backfill" {
  name              = "/ecs/${local.name}/embedding-backfill"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "backfill" {
  family                   = "${local.name}-embedding-backfill"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = local.core.app_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name                   = "embedding-backfill"
    image                  = "${local.core.ecr_repository_urls["contextledger-api"]}:${var.image_tag}"
    essential              = true
    command                = ["python", "-m", "app.workers.embeddings", "--drain", tostring(var.max_batches)]
    readonlyRootFilesystem = true
    user                   = "10001:10001"
    linuxParameters        = { capabilities = { drop = ["ALL"] }, initProcessEnabled = true }
    mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    environment = [
      { name = "CONTEXTLEDGER_ENVIRONMENT", value = "staging" },
      { name = "CONTEXTLEDGER_PROCESS_ROLE", value = "batch" },
      { name = "CONTEXTLEDGER_AUTH_MODE", value = "jwt" },
      { name = "CONTEXTLEDGER_LOG_JSON", value = "true" },
      { name = "CONTEXTLEDGER_METRICS_ENABLED", value = "false" },
      { name = "CONTEXTLEDGER_DB_HOST", value = local.core.db_endpoint },
      { name = "CONTEXTLEDGER_DB_NAME", value = "contextledger" },
      { name = "CONTEXTLEDGER_EMBEDDING_PROVIDER", value = "openai" },
    ]
    secrets = [
      { name = "CONTEXTLEDGER_DB_USER", valueFrom = "${local.db_secret_arn}:username::" },
      { name = "CONTEXTLEDGER_DB_PASSWORD", valueFrom = "${local.db_secret_arn}:password::" },
      { name = "CONTEXTLEDGER_OPENAI_API_KEY", valueFrom = local.openai_arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.backfill.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "backfill"
      }
    }
  }])

  volume {
    name = "tmp"
  }
}

# --- schedule (disabled by default) ---------------------------------------------------------

data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-batch-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["ecs:RunTask"]
    resources = [aws_ecs_task_definition.backfill.arn_without_revision, "${aws_ecs_task_definition.backfill.arn_without_revision}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.batch.arn]
    }
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, local.core.app_role_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "run-backfill"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "backfill" {
  name                = "${local.name}-embedding-backfill"
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"
  schedule_expression = var.schedule_expression
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_ecs_cluster.batch.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.backfill.arn
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = local.subnet_ids
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = !local.private
      }
    }
    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

# --- alert on failure -----------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "backfill_failed" {
  name        = "${local.name}-embedding-backfill-failed"
  description = "The embedding backfill task stopped with a non-zero exit code"
  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.batch.arn]
      lastStatus = ["STOPPED"]
      containers = { exitCode = [{ "anything-but" = 0 }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "backfill_failed" {
  rule = aws_cloudwatch_event_rule.backfill_failed.name
  arn  = local.core.alerts_topic_arn
}

# --- outputs --------------------------------------------------------------------------------

output "cluster_name" {
  value = aws_ecs_cluster.batch.name
}

output "task_definition" {
  value = aws_ecs_task_definition.backfill.family
}

output "run_task_network_configuration" {
  description = "Pass to `aws ecs run-task --network-configuration` (make batch-run does this)."
  value = jsonencode({
    awsvpcConfiguration = {
      subnets        = local.subnet_ids
      securityGroups = [aws_security_group.task.id]
      assignPublicIp = local.private ? "DISABLED" : "ENABLED"
    }
  })
}

output "log_group" {
  value = aws_cloudwatch_log_group.backfill.name
}
