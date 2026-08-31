# The deployment package.
#
# `source_dir = "../../src"` makes the archive root `reckon/`, so handler strings
# resolve as `reckon.aws.receiver.handler` with no build step, no layer and no
# container image. That works only because the runtime dependencies are zero and
# boto3 ships in the Lambda runtime (`PLAN.md` §3) — the moment a third-party
# package is added, this file needs a build pipeline instead.
data "archive_file" "source" {
  type        = "zip"
  source_dir  = "${path.module}/../../src"
  output_path = "${path.module}/.build/reckon.zip"
  # Verified against the archive provider rather than assumed: without the
  # explicit `__pycache__/**` and `.DS_Store` patterns, both end up in the
  # deployment package.
  excludes = [
    "**/__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/.DS_Store",
    ".DS_Store",
  ]
}

data "aws_caller_identity" "current" {}

locals {
  # Parameters live under one path so a single IAM statement covers the set.
  # Must match `reckon.aws.secrets.DEFAULT_PREFIX`.
  ssm_prefix   = "/${var.name}"
  ssm_arn_glob = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_prefix}/*"
}
