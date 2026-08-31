# One table for both stores: TOKEN#{service} and LOG#{activityId}, partition key
# only. On-demand because the load is a handful of writes a day and provisioned
# capacity would cost more than the traffic is worth.
resource "aws_dynamodb_table" "store" {
  name         = var.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  # Log entries carry a `ttl`; token records deliberately do not, so they are
  # never expired. Expiring a token would silently deauthorise the pipeline.
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }
}
