output "webhook_url" {
  description = "Register this with Google Health as the subscriber endpoint. See scripts/subscribe.py."
  value       = aws_lambda_function_url.receiver.function_url
}

output "table_name" {
  description = "The DynamoDB table holding tokens and the processed-activity log."
  value       = aws_dynamodb_table.store.name
}

output "queue_url" {
  description = "The work queue."
  value       = aws_sqs_queue.work.url
}

output "dead_letter_queue_url" {
  description = "Messages here are activities that did not reach Strava."
  value       = aws_sqs_queue.dead_letter.url
}

output "ssm_parameters" {
  description = "Secrets to populate before the first run; Terraform creates them empty."
  value       = [for p in aws_ssm_parameter.secret : p.name]
}

output "alarm_topic_arn" {
  description = "Subscribe to this to be told when something needs a human."
  value       = aws_sns_topic.alarms.arn
}
