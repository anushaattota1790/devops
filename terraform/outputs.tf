output "app_url" {
  value = "https://${var.domain_name}"
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service" {
  value = aws_ecs_service.app.name
}

output "github_actions_role_arn" {
  value = aws_iam_role.github_actions.arn
}
