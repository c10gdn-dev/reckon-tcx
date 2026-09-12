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

  # The inventory index. Its partition key is a constant, which is normally one
  # hot partition and is right here: the table holds one person's activities, and
  # the question it answers -- every activity in this window, oldest first -- is
  # otherwise a scan across the tokens and the whole processed log.
  attribute {
    name = "kind"
    type = "S"
  }

  attribute {
    name = "starts"
    type = "S"
  }

  global_secondary_index {
    name            = "kind-starts-index"
    hash_key        = "kind"
    range_key       = "starts"
    projection_type = "ALL"
  }

  # Log entries carry a `ttl`; token and inventory records deliberately do not,
  # so they are never expired. Expiring a token would silently deauthorise the
  # pipeline; expiring an inventory record would make Reckon forget an activity
  # exists and upload it a second time.
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }
}
