# Created explicitly rather than left to Lambda's implicit creation, so retention
# is bounded. An implicitly-created group never expires and quietly accrues cost.
resource "aws_cloudwatch_log_group" "receiver" {
  name              = "/aws/lambda/${var.name}-receiver"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${var.name}-worker"
  retention_in_days = var.log_retention_days
}

resource "aws_sns_topic" "alarms" {
  name = "${var.name}-alarms"
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count = var.alarm_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# Anything in the dead-letter queue is an activity that did not reach Strava
# after three attempts. There is no volume here for a threshold to smooth, so
# the alarm fires on a single message.
resource "aws_cloudwatch_metric_alarm" "dead_letter" {
  alarm_name          = "${var.name}-dead-letter"
  alarm_description   = "A message failed three times and was dead-lettered; an activity has not reached Strava."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.dead_letter.name
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# The failure mode a published-but-unverified OAuth client is most likely to hit.
# `AuthorisationExpired` means someone must re-run scripts/authorize.py; nothing
# automated can recover from it, so it needs to reach a human rather than sit in
# the log.
resource "aws_cloudwatch_log_metric_filter" "authorisation_expired" {
  name           = "${var.name}-authorisation-expired"
  log_group_name = aws_cloudwatch_log_group.worker.name
  pattern        = "\"authorisation is no longer valid\""

  metric_transformation {
    name          = "AuthorisationExpired"
    namespace     = "Reckon"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "authorisation_expired" {
  alarm_name          = "${var.name}-authorisation-expired"
  alarm_description   = "The Google or Strava authorisation has lapsed. Re-run scripts/authorize.py; this cannot be automated."
  namespace           = "Reckon"
  metric_name         = "AuthorisationExpired"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_log_group" "warden" {
  name              = "/aws/lambda/${var.name}-warden"
  retention_in_days = var.log_retention_days
}

# The grant is close to expiring. Distinct from the dead-letter alarm, and
# deliberately so: that one means an activity did not reach Strava, this one
# means one will stop reaching it soon. Folding them together would make the
# signal that something is *missing* fire when nothing is.
#
# Driven by a metric filter on the warden's own log rather than by the function
# failing, because an expiring grant is news and not a fault -- raising would put
# it on the dead-letter queue beside activities that genuinely failed.
resource "aws_cloudwatch_log_metric_filter" "grant_expiring" {
  name           = "${var.name}-grant-expiring"
  log_group_name = aws_cloudwatch_log_group.warden.name
  pattern        = "{ $.warn IS TRUE }"

  metric_transformation {
    name      = "GrantExpiring"
    namespace = var.name
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "grant_expiring" {
  alarm_name          = "${var.name}-grant-expiring"
  alarm_description   = "The Google authorisation is within a day of expiring. Re-run scripts/authorize.py google --profile testing --table ${var.name}."
  namespace           = var.name
  metric_name         = "GrantExpiring"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}
