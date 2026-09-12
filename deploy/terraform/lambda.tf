locals {
  # Non-secret configuration only. Secrets are read from SSM at run time, never
  # placed here: an environment variable is readable by anyone holding
  # `lambda:GetFunction`, and would also land in Terraform state.
  common_environment = {
    RECKON_TABLE     = aws_dynamodb_table.store.name
    RECKON_QUEUE_URL = aws_sqs_queue.work.url
    # Which Google OAuth client this deployment authenticates against. `testing`
    # is an unpublished client: it may hold the Restricted heart-rate scope, so
    # Relative Effort works, and its grant expires after seven days. `published`
    # is the reverse. See PLAN.md 13.1.
    RECKON_GOOGLE_PROFILE = var.google_profile
  }
}

resource "aws_lambda_function" "receiver" {
  function_name = "${var.name}-receiver"
  role          = aws_iam_role.receiver.arn
  handler       = "reckon.aws.receiver.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]

  filename         = data.archive_file.source.output_path
  source_code_hash = data.archive_file.source.output_base64sha256

  # It authenticates, enqueues and returns. Google treats a slow reply as a
  # failed delivery, so this is sized to be quick rather than capable.
  timeout     = 10
  memory_size = 128

  environment {
    variables = local.common_environment
  }

  depends_on = [aws_cloudwatch_log_group.receiver]
}

resource "aws_lambda_function_url" "receiver" {
  function_name = aws_lambda_function.receiver.function_name

  # No AWS authentication, deliberately: Google cannot sign requests with SigV4.
  # The shared secret in the Authorization header is the authentication, checked
  # with `hmac.compare_digest`. This is stated loudly in the README because an
  # unauthenticated Function URL is otherwise alarming to find.
  authorization_type = "NONE"
}

resource "aws_lambda_function" "worker" {
  function_name = "${var.name}-worker"
  role          = aws_iam_role.worker.arn
  handler       = "reckon.aws.worker.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]

  filename         = data.archive_file.source.output_path
  source_code_hash = data.archive_file.source.output_base64sha256

  timeout     = var.worker_timeout_seconds
  memory_size = 256

  environment {
    variables = local.common_environment
  }

  depends_on = [aws_cloudwatch_log_group.worker]
}

resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn = aws_sqs_queue.work.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 1

  # Concurrency is bounded here, NOT with `reserved_concurrent_executions = 1` on
  # the function. Reserved concurrency of 1 has a known failure mode with SQS:
  # the poller scales independently of the throttle, throttled deliveries expire
  # their visibility timeout, receive counts climb, and healthy messages poison
  # into the DLQ. Correctness under the residual concurrency of 2 comes from the
  # token compare-and-swap (`PLAN.md` §9).
  scaling_config {
    maximum_concurrency = 2
  }
}


# The warden: a daily look at how old the stored grant is.
#
# Only worth its keep on the `testing` profile, whose grant Google expires after
# seven days -- but it costs one invocation a day either way, and a deployment
# that switches profile should not also have to remember to add a schedule.
resource "aws_lambda_function" "warden" {
  function_name = "${var.name}-warden"
  role          = aws_iam_role.warden.arn
  handler       = "reckon.aws.warden.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]

  filename         = data.archive_file.source.output_path
  source_code_hash = data.archive_file.source.output_base64sha256

  timeout     = 10
  memory_size = 128

  environment {
    variables = local.common_environment
  }

  depends_on = [aws_cloudwatch_log_group.warden]
}

resource "aws_cloudwatch_event_rule" "warden" {
  name                = "${var.name}-warden-daily"
  description         = "Check how old the Google grant is"
  schedule_expression = "cron(0 18 * * ? *)"
}

resource "aws_cloudwatch_event_target" "warden" {
  rule      = aws_cloudwatch_event_rule.warden.name
  target_id = "warden"
  arn       = aws_lambda_function.warden.arn
}

resource "aws_lambda_permission" "warden" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.warden.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.warden.arn
}
