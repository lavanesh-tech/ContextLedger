# Secret *containers* only. Values are set out of band (aws secretsmanager
# put-secret-value), so they never enter Terraform code or state. The database
# password is a separate secret managed by RDS itself.

locals {
  app_secrets = {
    jwt-signing-key = "ES256 private key (PEM) for CONTEXTLEDGER_JWT_SIGNING_KEY"
    openai-api-key  = "OpenAI API key for embeddings and generation (optional)"
    metrics-token   = "Bearer token Prometheus uses to scrape /metrics"
  }
}

resource "aws_secretsmanager_secret" "app" {
  for_each                = local.app_secrets
  name                    = "${var.project}/${var.environment}/${each.key}"
  description             = each.value
  recovery_window_in_days = var.secret_recovery_window_days
}
