# The five secrets, as SecureStrings. Terraform creates the parameters but never
# their values: `ignore_changes` on `value` means the placeholder is written once
# and every real value is put in by hand or by CI, so no plaintext secret ever
# enters Terraform state.
#
# Set them with, for example:
#   aws ssm put-parameter --name /reckon/google_client_secret \
#     --type SecureString --overwrite --value "$SECRET"
resource "aws_ssm_parameter" "secret" {
  for_each = toset([
    "google_client_id",
    "google_client_secret",
    "strava_client_id",
    "strava_client_secret",
    "webhook_secret",
  ])

  name  = "${local.ssm_prefix}/${each.key}"
  type  = "SecureString"
  value = "replace-me"

  lifecycle {
    ignore_changes = [value]
  }
}
