variable "aws_region" {
  type    = string
  default = "ap-southeast-2"
}

variable "project" {
  type    = string
  default = "healthz-app"
}

variable "domain_name" {
  description = "FQDN the app is served on, e.g. healthz.example.com"
  type        = string
}

variable "hosted_zone_name" {
  description = "Existing Route53 public hosted zone that contains domain_name, e.g. example.com"
  type        = string
}

variable "github_repository" {
  description = "GitHub repo allowed to deploy, in owner/name form"
  type        = string
}

variable "create_github_oidc_provider" {
  description = "Set false if the account already has the GitHub OIDC provider"
  type        = bool
  default     = true
}

variable "image_tag" {
  description = "Image tag used for the first apply only; CD manages it afterwards"
  type        = string
  default     = "bootstrap"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "container_port" {
  type    = number
  default = 8443
}

variable "task_cpu" {
  type    = number
  default = 256
}

variable "task_memory" {
  type    = number
  default = 512
}

variable "desired_count" {
  type    = number
  default = 2
}

variable "max_count" {
  type    = number
  default = 4
}

variable "enable_deletion_protection" {
  description = "Enable on the ALB for real environments; off so the demo is easy to tear down"
  type        = bool
  default     = false
}
