variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "eu-west-2"
}

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "reckon"
}

variable "log_retention_days" {
  description = "CloudWatch log retention. Short by default; these logs are for debugging a failure, not for history."
  type        = number
  default     = 14
}

variable "alarm_email" {
  description = "Address to notify when a message reaches the dead-letter queue. Empty disables the subscription, leaving the alarm and topic in place."
  type        = string
  default     = ""
}

variable "worker_timeout_seconds" {
  description = "Worker Lambda timeout. The queue's visibility timeout is derived from this."
  type        = number

  # 300, not 60. The worker polls Strava's asynchronous upload with a bounded
  # sleep of up to 62 s per activity, and a notification can carry several -- so
  # 60 s could not finish two. Idle time is not billed on a queue this quiet, so
  # the headroom costs nothing and buys the difference between draining and
  # dead-lettering.
  default = 300
}


variable "google_profile" {
  description = <<-EOT
    Which Google OAuth client this deployment authenticates against.

    "testing"   an unpublished client. May hold the Restricted heart-rate scope,
                so Strava shows Relative Effort -- at the cost of a grant that
                expires after seven days and must be renewed by hand.
    "published" a published client. No heart rate, and no maintenance.

    Named for the client's publishing status because that is what a person can
    check: the Audience page in the Cloud console says "Testing" or "In
    production".
  EOT
  type        = string
  default     = "published"

  validation {
    condition     = contains(["testing", "published"], var.google_profile)
    error_message = "google_profile must be \"testing\" or \"published\"."
  }
}
