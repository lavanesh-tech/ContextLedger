# PostgreSQL 17 (pgvector is available as an RDS extension: CREATE EXTENSION vector,
# run by migration 0001). Private, encrypted, TLS required, master password generated
# and rotated by RDS in Secrets Manager: it never appears in Terraform state.

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "PostgreSQL: only from application workloads"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "PostgreSQL from the app security group"
}

resource "aws_db_parameter_group" "postgres" {
  name   = "${local.name}-pg17"
  family = "postgres17"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage_gb
  max_allocated_storage = var.db_allocated_storage_gb * 2
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name                     = "contextledger"
  username                    = "contextledger"
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period         = var.db_backup_retention_days
  deletion_protection             = var.db_deletion_protection
  skip_final_snapshot             = var.db_skip_final_snapshot
  final_snapshot_identifier       = var.db_skip_final_snapshot ? null : "${local.name}-final"
  copy_tags_to_snapshot           = true
  auto_minor_version_upgrade      = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
  performance_insights_enabled    = false

  # The exported-log group must exist (with its retention) before RDS creates it.
  depends_on = [aws_cloudwatch_log_group.rds]
}
