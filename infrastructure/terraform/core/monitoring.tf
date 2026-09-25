# Log groups with a retention limit (logs never grow unbounded), an SNS topic for
# alerts, database alarms and a monthly AWS Budget.

resource "aws_cloudwatch_log_group" "app" {
  for_each          = toset(["api", "worker", "graph-projector", "event-relay", "event-consumers"])
  name              = "/${var.project}/${var.environment}/${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "rds" {
  name              = "/aws/rds/instance/${local.name}/postgresql"
  retention_in_days = var.log_retention_days
}

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  db_alarms = {
    cpu-high = {
      metric    = "CPUUtilization"
      operator  = "GreaterThanThreshold"
      threshold = 80
      statistic = "Average"
    }
    free-storage-low = {
      metric    = "FreeStorageSpace"
      operator  = "LessThanThreshold"
      threshold = 2 * 1024 * 1024 * 1024
      statistic = "Minimum"
    }
    connections-high = {
      metric    = "DatabaseConnections"
      operator  = "GreaterThanThreshold"
      threshold = 60
      statistic = "Maximum"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "db" {
  for_each            = local.db_alarms
  alarm_name          = "${local.name}-db-${each.key}"
  namespace           = "AWS/RDS"
  metric_name         = each.value.metric
  statistic           = each.value.statistic
  comparison_operator = each.value.operator
  threshold           = each.value.threshold
  period              = 300
  evaluation_periods  = 2
  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.identifier
  }
  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
