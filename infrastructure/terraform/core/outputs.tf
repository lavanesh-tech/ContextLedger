output "vpc_id" {
  value = aws_vpc.main.id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "db_endpoint" {
  value = aws_db_instance.main.address
}

output "db_master_secret_arn" {
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
  description = "RDS-managed secret with the master username and password."
}

output "evidence_bucket" {
  value = aws_s3_bucket.evidence.bucket
}

output "ecr_repository_urls" {
  value = { for k, r in aws_ecr_repository.app : k => r.repository_url }
}

output "app_secret_arns" {
  value = { for k, s in aws_secretsmanager_secret.app : k => s.arn }
}

output "app_role_arn" {
  value = aws_iam_role.app.arn
}

output "ci_role_arn" {
  value       = aws_iam_role.ci.arn
  description = "Set as the AWS_ROLE_ARN repository variable for the ECR push workflow."
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
