terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }

  # Remote state (recommended). Create the bucket + lock table once, then uncomment:
  # backend "s3" {
  #   bucket         = "<your-tfstate-bucket>"
  #   key            = "healthz-app/terraform.tfstate"
  #   region         = "ap-southeast-2"
  #   dynamodb_table = "<your-tfstate-lock-table>"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}
