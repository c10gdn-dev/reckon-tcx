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
  default     = 60
}
