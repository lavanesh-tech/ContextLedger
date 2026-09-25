terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  # Partial configuration: `terraform init -backend-config=backend.hcl`
  # (copy backend.hcl.example; the bucket name comes from the bootstrap stack).
  backend "s3" {}
}

provider "aws" {
  region = var.region
  # Every resource is tagged: cost allocation and teardown checks filter on these.
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = var.owner
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}
