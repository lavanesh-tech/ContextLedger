# EKS for demos (Phase 23). Reads the core stack's outputs from its remote state.
#
# Cost: the control plane is billed per hour, plus the nodes, plus the NAT gateway
# the private nodes need to pull images. Create it for a demo and destroy it after
# (docs/EKS.md, docs/AWS_TEARDOWN.md).

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = var.owner
      Stack       = "eks"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "contextledger"
}

variable "environment" {
  type    = string
  default = "demo"
}

variable "owner" {
  type = string
}

variable "state_bucket" {
  type        = string
  description = "The bootstrap state bucket (same as in backend.hcl)."
}

variable "kubernetes_version" {
  type    = string
  default = "1.33"
}

variable "node_instance_types" {
  type        = list(string)
  default     = ["t4g.medium"]
  description = "Graviton (arm64); the images are built for linux/arm64 and linux/amd64."
}

variable "node_capacity_type" {
  type        = string
  default     = "SPOT"
  description = "SPOT is cheaper and fine for a demo; ON_DEMAND for stability."
}

variable "node_count" {
  type    = number
  default = 2
}

variable "api_allowed_cidrs" {
  type        = list(string)
  description = "CIDRs allowed to reach the Kubernetes API endpoint, e.g. [\"203.0.113.7/32\"] (your IP)."
}

data "terraform_remote_state" "core" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "core/terraform.tfstate"
    region = var.region
  }
}

locals {
  name = "${var.project}-${var.environment}"
  core = data.terraform_remote_state.core.outputs
}

check "nat_gateway_for_private_nodes" {
  assert {
    condition     = local.core.nat_gateway_enabled
    error_message = "Nodes run in private subnets and need the NAT gateway to pull images: apply the core stack with enable_nat_gateway = true first."
  }
}

# --- cluster --------------------------------------------------------------------------

data "aws_iam_policy_document" "cluster_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${local.name}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.cluster_trust.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${local.name}/cluster"
  retention_in_days = 7
}

resource "aws_eks_cluster" "main" {
  name     = local.name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = local.core.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.api_allowed_cidrs
  }

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  depends_on = [aws_iam_role_policy_attachment.cluster, aws_cloudwatch_log_group.cluster]
}

# --- nodes ----------------------------------------------------------------------------

data "aws_iam_policy_document" "node_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${local.name}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.node_trust.json
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])
  role       = aws_iam_role.node.name
  policy_arn = each.value
}

resource "aws_eks_node_group" "main" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${local.name}-default"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = local.core.private_subnet_ids
  ami_type        = "AL2023_ARM_64_STANDARD"
  instance_types  = var.node_instance_types
  capacity_type   = var.node_capacity_type
  disk_size       = 20

  scaling_config {
    desired_size = var.node_count
    min_size     = 1
    max_size     = var.node_count + 1
  }

  update_config {
    max_unavailable = 1
  }

  depends_on = [aws_iam_role_policy_attachment.node]
}

# --- add-ons --------------------------------------------------------------------------

resource "aws_eks_addon" "vpc_cni" {
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "vpc-cni"
  # Enforce the NetworkPolicies in infrastructure/kubernetes.
  configuration_values = jsonencode({ enableNetworkPolicy = "true" })
}

resource "aws_eks_addon" "others" {
  for_each     = toset(["coredns", "kube-proxy", "eks-pod-identity-agent"])
  cluster_name = aws_eks_cluster.main.name
  addon_name   = each.key
  depends_on   = [aws_eks_node_group.main]
}

# --- workload identity and database access ---------------------------------------------

resource "aws_eks_pod_identity_association" "app" {
  cluster_name    = aws_eks_cluster.main.name
  namespace       = "contextledger"
  service_account = "contextledger-app"
  role_arn        = local.core.app_role_arn
}

resource "aws_vpc_security_group_ingress_rule" "db_from_cluster" {
  security_group_id            = local.core.db_security_group_id
  referenced_security_group_id = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "PostgreSQL from EKS nodes and pods"
}

output "cluster_name" {
  value = aws_eks_cluster.main.name
}

output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${aws_eks_cluster.main.name}"
}
