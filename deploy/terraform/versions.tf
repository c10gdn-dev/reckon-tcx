terraform {
  # 1.10 is the floor imposed by S3 native state locking (`use_lockfile`), which
  # is what lets the backend work without the legacy DynamoDB lock table.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "reckon"
      ManagedBy = "terraform"
    }
  }
}
