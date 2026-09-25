# Athena over retrieval-evaluation results (Phase 26).
#
# Scope is deliberately small: the only analytics data this project produces is its
# evaluation output, exported as JSON Lines (make analytics-export) and synced to a
# private bucket. Glue tables use partition projection on run_date, so no crawler and
# no MSCK REPAIR are needed. The workgroup enforces an encrypted result location and a
# per-query scan limit. Athena bills per data scanned; with a few KB of data a query
# costs a fraction of a cent, and nothing is billed while idle apart from S3 storage.

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
      Stack       = "analytics"
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

variable "bytes_scanned_cutoff" {
  type        = number
  default     = 104857600
  description = "Per-query scan limit in bytes (100 MiB); queries above it are cancelled."
}

data "aws_caller_identity" "current" {}

locals {
  name   = "${var.project}-${var.environment}"
  db     = replace("${local.name}_analytics", "-", "_")
  prefix = "retrieval"
}

# --- bucket -------------------------------------------------------------------------------

resource "aws_s3_bucket" "analytics" {
  bucket        = "${local.name}-analytics-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # derived data only: re-exportable from evaluation/results
}

resource "aws_s3_bucket_public_access_block" "analytics" {
  bucket                  = aws_s3_bucket.analytics.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "analytics" {
  bucket = aws_s3_bucket.analytics.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "analytics" {
  bucket = aws_s3_bucket.analytics.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "analytics" {
  bucket = aws_s3_bucket.analytics.id
  rule {
    id     = "expire-athena-query-results"
    status = "Enabled"
    filter {
      prefix = "athena-results/"
    }
    expiration {
      days = 7
    }
  }
}

data "aws_iam_policy_document" "tls_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.analytics.arn, "${aws_s3_bucket.analytics.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "analytics" {
  bucket = aws_s3_bucket.analytics.id
  policy = data.aws_iam_policy_document.tls_only.json
}

# --- Glue catalog ---------------------------------------------------------------------------

resource "aws_glue_catalog_database" "analytics" {
  name        = local.db
  description = "ContextLedger evaluation results (SYNTHETIC datasets; see docs/ATHENA.md)"
}

locals {
  tables = {
    retrieval_runs = [
      { name = "run_id", type = "string" },
      { name = "run_timestamp", type = "string" },
      { name = "commit_sha", type = "string" },
      { name = "working_tree_dirty", type = "boolean" },
      { name = "dataset_name", type = "string" },
      { name = "dataset_version", type = "string" },
      { name = "synthetic", type = "boolean" },
      { name = "embedding_model", type = "string" },
      { name = "configuration", type = "string" },
      { name = "cases", type = "int" },
      { name = "cases_with_relevant_facts", type = "int" },
      { name = "mrr", type = "double" },
      { name = "recall_at_1", type = "double" },
      { name = "recall_at_3", type = "double" },
      { name = "recall_at_5", type = "double" },
      { name = "recall_at_10", type = "double" },
      { name = "precision_at_1", type = "double" },
      { name = "precision_at_3", type = "double" },
      { name = "precision_at_5", type = "double" },
      { name = "precision_at_10", type = "double" },
      { name = "ndcg_at_1", type = "double" },
      { name = "ndcg_at_3", type = "double" },
      { name = "ndcg_at_5", type = "double" },
      { name = "ndcg_at_10", type = "double" },
      { name = "temporal_correctness", type = "double" },
      { name = "authorization_correctness", type = "double" },
      { name = "forbidden_results", type = "int" },
      { name = "latency_ms_p50", type = "double" },
    ]
    retrieval_cases = [
      { name = "run_id", type = "string" },
      { name = "run_timestamp", type = "string" },
      { name = "commit_sha", type = "string" },
      { name = "working_tree_dirty", type = "boolean" },
      { name = "dataset_name", type = "string" },
      { name = "dataset_version", type = "string" },
      { name = "synthetic", type = "boolean" },
      { name = "embedding_model", type = "string" },
      { name = "configuration", type = "string" },
      { name = "case_id", type = "string" },
      { name = "category", type = "string" },
      { name = "relevant_count", type = "int" },
      { name = "returned_count", type = "int" },
      { name = "first_relevant_rank", type = "int" },
      { name = "reciprocal_rank", type = "double" },
      { name = "latency_ms", type = "double" },
      { name = "vector_search", type = "string" },
      { name = "violation_count", type = "int" },
    ]
  }
}

resource "aws_glue_catalog_table" "tables" {
  for_each      = local.tables
  name          = each.key
  database_name = aws_glue_catalog_database.analytics.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    classification                      = "json"
    "projection.enabled"                = "true"
    "projection.run_date.type"          = "date"
    "projection.run_date.format"        = "yyyy-MM-dd"
    "projection.run_date.range"         = "2026-01-01,NOW"
    "projection.run_date.interval"      = "1"
    "projection.run_date.interval.unit" = "DAYS"
    "storage.location.template"         = "s3://${aws_s3_bucket.analytics.bucket}/${local.prefix}/${each.key}/run_date=$${run_date}/"
  }

  partition_keys {
    name = "run_date"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.analytics.bucket}/${local.prefix}/${each.key}/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters            = { "ignore.malformed.json" = "false" }
    }

    dynamic "columns" {
      for_each = each.value
      content {
        name = columns.value.name
        type = columns.value.type
      }
    }
  }
}

# --- Athena ---------------------------------------------------------------------------------

resource "aws_athena_workgroup" "analytics" {
  name          = "${local.name}-analytics"
  force_destroy = true
  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = var.bytes_scanned_cutoff
    result_configuration {
      output_location = "s3://${aws_s3_bucket.analytics.bucket}/athena-results/"
      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}

locals {
  queries = {
    best-recall-at-5 = {
      description = "Which retrieval configuration produced the best Recall@5 (latest run first)?"
      sql         = <<-SQL
        SELECT run_date, run_id, configuration, recall_at_5, mrr, ndcg_at_5, synthetic
        FROM ${local.db}.retrieval_runs
        ORDER BY run_date DESC, recall_at_5 DESC, mrr DESC
        LIMIT 20
      SQL
    }
    mrr-by-category = {
      description = "Mean reciprocal rank per case category and configuration."
      sql         = <<-SQL
        SELECT configuration, category, count(*) AS cases,
               round(avg(reciprocal_rank), 4) AS mrr,
               round(approx_percentile(latency_ms, 0.5), 2) AS latency_ms_p50
        FROM ${local.db}.retrieval_cases
        WHERE relevant_count > 0
        GROUP BY configuration, category
        ORDER BY category, mrr DESC
      SQL
    }
    correctness-violations = {
      description = "Cases that returned forbidden results (temporal, revoked, tenant or privacy violations)."
      sql         = <<-SQL
        SELECT run_date, run_id, configuration, case_id, category, violation_count
        FROM ${local.db}.retrieval_cases
        WHERE violation_count > 0
        ORDER BY run_date DESC, violation_count DESC
      SQL
    }
    trend-by-commit = {
      description = "How MRR and Recall@5 moved across commits for each configuration."
      sql         = <<-SQL
        SELECT configuration, run_timestamp, substr(commit_sha, 1, 8) AS commit_sha,
               working_tree_dirty, mrr, recall_at_5
        FROM ${local.db}.retrieval_runs
        ORDER BY configuration, run_timestamp
      SQL
    }
  }
}

resource "aws_athena_named_query" "queries" {
  for_each    = local.queries
  name        = each.key
  description = each.value.description
  workgroup   = aws_athena_workgroup.analytics.name
  database    = aws_glue_catalog_database.analytics.name
  query       = each.value.sql
}

output "analytics_bucket" {
  value = aws_s3_bucket.analytics.bucket
}

output "upload_prefix" {
  value = "s3://${aws_s3_bucket.analytics.bucket}/${local.prefix}/"
}

output "workgroup" {
  value = aws_athena_workgroup.analytics.name
}

output "database" {
  value = aws_glue_catalog_database.analytics.name
}
