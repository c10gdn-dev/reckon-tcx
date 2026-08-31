# One role per function, each granting only what that function does. The receiver
# cannot touch DynamoDB at all: it authenticates, enqueues and returns, and an
# endpoint exposed to the internet with no AWS authentication should be able to
# do nothing else.

data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# --- receiver ---------------------------------------------------------------

resource "aws_iam_role" "receiver" {
  name               = "${var.name}-receiver"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

data "aws_iam_policy_document" "receiver" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.work.arn]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [local.ssm_arn_glob]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "receiver" {
  role   = aws_iam_role.receiver.id
  policy = data.aws_iam_policy_document.receiver.json
}

resource "aws_iam_role_policy_attachment" "receiver_logs" {
  role       = aws_iam_role.receiver.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# --- worker -----------------------------------------------------------------

resource "aws_iam_role" "worker" {
  name               = "${var.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

data "aws_iam_policy_document" "worker" {
  statement {
    # SendMessage as well as receive: the worker re-enqueues its own delayed
    # upload checks rather than sleeping.
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:SendMessage",
    ]
    resources = [aws_sqs_queue.work.arn]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.store.arn]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [local.ssm_arn_glob]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "worker" {
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_iam_role_policy_attachment" "worker_logs" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
