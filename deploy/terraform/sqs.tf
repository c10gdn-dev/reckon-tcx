# The dead-letter queue. A message lands here after three failed receives, which
# means three genuinely transient faults in a row — deterministic outcomes are
# recorded and the message deleted, so they never reach this.
resource "aws_sqs_queue" "dead_letter" {
  name                      = "${var.name}-dlq"
  message_retention_seconds = 1209600 # 14 days, the maximum
}

resource "aws_sqs_queue" "work" {
  name = var.name

  # At least six times the worker timeout, per PLAN.md §9. Too short and a
  # message becomes visible again while the worker is still handling it, so it
  # is processed twice and the receive count climbs towards the DLQ for no
  # reason.
  visibility_timeout_seconds = var.worker_timeout_seconds * 6

  # Long polling: fewer empty receives, and empty receives are billed.
  receive_wait_time_seconds = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dead_letter.arn
    maxReceiveCount     = 3
  })
}
