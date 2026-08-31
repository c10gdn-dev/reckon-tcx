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
