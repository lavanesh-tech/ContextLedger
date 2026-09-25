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
  validation {
    condition     = contains(["demo", "staging", "production"], var.environment)
    error_message = "environment must be demo, staging or production."
  }
}

variable "owner" {
  type        = string
  description = "Who to ask about these resources (a name or email), used as a tag."
}

variable "alert_email" {
  type        = string
  description = "Receives budget and CloudWatch alarm notifications (confirm the SNS subscription email)."
}

variable "monthly_budget_usd" {
  type        = number
  default     = 30
  description = "AWS Budget for this account; alerts at 50%, 80% and 100% (actual) and 100% (forecast)."
}

# --- network ---------------------------------------------------------------------

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "enable_nat_gateway" {
  type        = bool
  default     = false
  description = "One NAT gateway for private subnets. Billed per hour while it exists: keep false unless workloads in private subnets need the internet."
}

# --- database --------------------------------------------------------------------

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 20
}

variable "db_engine_version" {
  type    = string
  default = "17"
}

variable "db_backup_retention_days" {
  type    = number
  default = 1
}

variable "db_deletion_protection" {
  type        = bool
  default     = false
  description = "true blocks `terraform destroy` of the database. false suits a demo that is torn down."
}

variable "db_skip_final_snapshot" {
  type        = bool
  default     = false
  description = "false keeps a final snapshot on destroy (small storage cost, data can be restored)."
}

# --- teardown helpers ----------------------------------------------------------------

variable "force_destroy_storage" {
  type        = bool
  default     = true
  description = "Allow destroy to delete a non-empty evidence bucket and ECR repositories with images."
}

variable "secret_recovery_window_days" {
  type        = number
  default     = 0
  description = "0 deletes secrets immediately on destroy so the stack can be recreated with the same names."
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "github_repository" {
  type        = string
  default     = "lavanesh-tech/ContextLedger"
  description = "owner/repo allowed to assume the CI role through GitHub OIDC."
}
