import json

import pulumi
import pulumi_aws as aws


config = pulumi.Config()
aws_config = pulumi.Config("aws")
region = aws_config.require("region")
project = config.get("project") or "healthz-app"
domain_name = config.require("domainName")
hosted_zone_name = config.require("hostedZoneName")
github_repository = config.require("githubRepository")
github_oidc_subject = config.get("githubOidcSubject") or f"repo:{github_repository}:ref:refs/heads/main"
deploy_service = config.get_bool("deployService") or False
image_tag = config.get("imageTag")
if deploy_service and not image_tag:
    raise ValueError("Set imageTag to an existing ECR image tag before enabling deployService.")

tags = {"Project": project, "ManagedBy": "pulumi"}
def create_network():
    availability_zones = aws.get_availability_zones(state="available").names[:2]
    if len(availability_zones) < 2:
        raise ValueError("At least two available Availability Zones are required.")

    vpc = aws.ec2.Vpc(
        "vpc",
        cidr_block="10.20.0.0/16",
        enable_dns_support=True,
        enable_dns_hostnames=True,
        tags={**tags, "Name": project},
    )
    internet_gateway = aws.ec2.InternetGateway(
        "internet-gateway", vpc_id=vpc.id, tags={**tags, "Name": project}
    )
    public_subnets = [
        aws.ec2.Subnet(
            f"public-{index}",
            vpc_id=vpc.id,
            availability_zone=zone,
            cidr_block=f"10.20.{index}.0/24",
            map_public_ip_on_launch=False,
            tags={**tags, "Name": f"{project}-public-{zone}"},
        )
        for index, zone in enumerate(availability_zones)
    ]
    private_subnets = [
        aws.ec2.Subnet(
            f"private-{index}",
            vpc_id=vpc.id,
            availability_zone=zone,
            cidr_block=f"10.20.{index + 10}.0/24",
            tags={**tags, "Name": f"{project}-private-{zone}"},
        )
        for index, zone in enumerate(availability_zones)
    ]
    nat_ip = aws.ec2.Eip("nat-ip", domain="vpc", tags={**tags, "Name": project})
    nat = aws.ec2.NatGateway(
        "nat",
        allocation_id=nat_ip.id,
        subnet_id=public_subnets[0].id,
        tags={**tags, "Name": project},
        opts=pulumi.ResourceOptions(depends_on=[internet_gateway]),
    )
    public_routes = aws.ec2.RouteTable(
        "public-routes",
        vpc_id=vpc.id,
        routes=[{"cidr_block": "0.0.0.0/0", "gateway_id": internet_gateway.id}],
        tags={**tags, "Name": f"{project}-public"},
    )
    private_routes = aws.ec2.RouteTable(
        "private-routes",
        vpc_id=vpc.id,
        routes=[{"cidr_block": "0.0.0.0/0", "nat_gateway_id": nat.id}],
        tags={**tags, "Name": f"{project}-private"},
    )
    public_associations = [
        aws.ec2.RouteTableAssociation(
            f"public-association-{index}", subnet_id=subnet.id, route_table_id=public_routes.id
        )
        for index, subnet in enumerate(public_subnets)
    ]
    private_associations = [
        aws.ec2.RouteTableAssociation(
            f"private-association-{index}", subnet_id=subnet.id, route_table_id=private_routes.id
        )
        for index, subnet in enumerate(private_subnets)
    ]
    return vpc, public_subnets, private_subnets, public_associations, private_associations


def create_security_groups(vpc: aws.ec2.Vpc):
    alb_security_group = aws.ec2.SecurityGroup(
        "alb-security-group",
        name=f"{project}-alb",
        description="Public application load balancer",
        vpc_id=vpc.id,
        tags=tags,
    )
    service_security_group = aws.ec2.SecurityGroup(
        "service-security-group",
        name=f"{project}-service",
        description="ECS tasks",
        vpc_id=vpc.id,
        tags=tags,
    )
    for port in (80, 443):
        aws.vpc.SecurityGroupIngressRule(
            f"alb-ingress-{port}",
            security_group_id=alb_security_group.id,
            cidr_ipv4="0.0.0.0/0",
            ip_protocol="tcp",
            from_port=port,
            to_port=port,
        )
    aws.vpc.SecurityGroupEgressRule(
        "alb-to-service",
        security_group_id=alb_security_group.id,
        referenced_security_group_id=service_security_group.id,
        ip_protocol="tcp",
        from_port=8443,
        to_port=8443,
    )
    aws.vpc.SecurityGroupIngressRule(
        "service-from-alb",
        security_group_id=service_security_group.id,
        referenced_security_group_id=alb_security_group.id,
        ip_protocol="tcp",
        from_port=8443,
        to_port=8443,
    )
    aws.vpc.SecurityGroupEgressRule(
        "service-https-egress",
        security_group_id=service_security_group.id,
        cidr_ipv4="0.0.0.0/0",
        ip_protocol="tcp",
        from_port=443,
        to_port=443,
    )
    return alb_security_group, service_security_group


def create_ecr_repository():
    repository = aws.ecr.Repository(
        "repository",
        name=project,
        image_tag_mutability="IMMUTABLE",
        image_scanning_configuration={"scan_on_push": True},
        encryption_configurations=[{"encryption_type": "AES256"}],
        tags=tags,
    )
    aws.ecr.LifecyclePolicy(
        "repository-lifecycle",
        repository=repository.name,
        policy=json.dumps({
            "rules": [{
                "rulePriority": 1,
                "description": "Keep the last 20 images",
                "selection": {
                    "tagStatus": "any",
                    "countType": "imageCountMoreThan",
                    "countNumber": 20,
                },
                "action": {"type": "expire"},
            }]
        }),
    )
    return repository


def create_iam_roles(repository: aws.ecr.Repository):
    assume_ecs_tasks = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    })
    execution_role = aws.iam.Role(
        "task-execution-role", name=f"{project}-task-execution", assume_role_policy=assume_ecs_tasks, tags=tags
    )
    execution_policy = aws.iam.RolePolicyAttachment(
        "task-execution-policy",
        role=execution_role.name,
        policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
    )
    task_role = aws.iam.Role(
        "task-role", name=f"{project}-task", assume_role_policy=assume_ecs_tasks, tags=tags
    )

    github_oidc_url = "https://token.actions.githubusercontent.com"
    if config.get_bool("useExistingGithubOidcProvider"):
        oidc_arn = aws.iam.get_open_id_connect_provider_output(url=github_oidc_url).arn
    else:
        oidc = aws.iam.OpenIdConnectProvider(
            "github-oidc",
            url=github_oidc_url,
            client_id_lists=["sts.amazonaws.com"],
            thumbprint_lists=["6938fd4d98bab03faadb97b34396831e3780aea1"],
            tags=tags,
        )
        oidc_arn = oidc.arn
    github_assume = oidc_arn.apply(lambda arn: json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Federated": arn},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {
                "StringEquals": {
                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                    "token.actions.githubusercontent.com:sub": github_oidc_subject,
                }
            },
        }],
    }))
    github_role = aws.iam.Role(
        "github-role", name=f"{project}-github-actions", assume_role_policy=github_assume, tags=tags
    )
    account_id = aws.get_caller_identity().account_id
    github_policy = pulumi.Output.all(repository.arn, execution_role.arn, task_role.arn).apply(
        lambda arns: json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
                {
                    "Effect": "Allow",
                    "Action": [
                        "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage",
                        "ecr:CompleteLayerUpload", "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer",
                        "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart",
                    ],
                    "Resource": arns[0],
                },
                {
                    "Effect": "Allow",
                    "Action": ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["ecs:DescribeServices", "ecs:UpdateService"],
                    "Resource": f"arn:aws:ecs:{region}:{account_id}:service/{project}/{project}",
                },
                {
                    "Effect": "Allow",
                    "Action": "iam:PassRole",
                    "Resource": [arns[1], arns[2]],
                    "Condition": {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
                },
            ],
        })
    )
    aws.iam.RolePolicy("github-policy", name="deploy", role=github_role.id, policy=github_policy)
    return execution_role, execution_policy, task_role, github_role


def create_load_balancer(vpc: aws.ec2.Vpc, public_subnets: list[aws.ec2.Subnet], alb_security_group: aws.ec2.SecurityGroup):
    zone = aws.route53.get_zone(name=hosted_zone_name, private_zone=False)
    certificate = aws.acm.Certificate(
        "certificate", domain_name=domain_name, validation_method="DNS", tags=tags
    )
    validation_record = aws.route53.Record(
        "certificate-validation-record",
        zone_id=zone.zone_id,
        name=certificate.domain_validation_options.apply(lambda options: options[0].resource_record_name),
        type=certificate.domain_validation_options.apply(lambda options: options[0].resource_record_type),
        records=[certificate.domain_validation_options.apply(lambda options: options[0].resource_record_value)],
        ttl=60,
    )
    certificate_validation = aws.acm.CertificateValidation(
        "certificate-validation",
        certificate_arn=certificate.arn,
        validation_record_fqdns=[validation_record.fqdn],
    )
    load_balancer = aws.lb.LoadBalancer(
        "load-balancer",
        name=project,
        internal=False,
        load_balancer_type="application",
        security_groups=[alb_security_group.id],
        subnets=[subnet.id for subnet in public_subnets],
        drop_invalid_header_fields=True,
        tags=tags,
    )
    target_group = aws.lb.TargetGroup(
        "target-group",
        name=project,
        vpc_id=vpc.id,
        target_type="ip",
        protocol="HTTPS",
        port=8443,
        deregistration_delay=30,
        health_check={
            "protocol": "HTTPS",
            "path": "/healthz",
            "matcher": "200",
            "interval": 15,
            "timeout": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 3,
        },
        tags=tags,
    )
    https_listener = aws.lb.Listener(
        "https-listener",
        load_balancer_arn=load_balancer.arn,
        port=443,
        protocol="HTTPS",
        ssl_policy="ELBSecurityPolicy-TLS13-1-2-2021-06",
        certificate_arn=certificate_validation.certificate_arn,
        default_actions=[{"type": "forward", "target_group_arn": target_group.arn}],
    )
    aws.lb.Listener(
        "http-redirect",
        load_balancer_arn=load_balancer.arn,
        port=80,
        protocol="HTTP",
        default_actions=[{
            "type": "redirect",
            "redirect": {"port": "443", "protocol": "HTTPS", "status_code": "HTTP_301"},
        }],
    )
    aws.route53.Record(
        "application-dns",
        zone_id=zone.zone_id,
        name=domain_name,
        type="A",
        aliases=[{
            "name": load_balancer.dns_name,
            "zone_id": load_balancer.zone_id,
            "evaluate_target_health": True,
        }],
    )
    return load_balancer, target_group, https_listener


def create_cluster_and_log_group():
    log_group = aws.cloudwatch.LogGroup(
        "log-group", name=f"/ecs/{project}", retention_in_days=30, tags=tags
    )
    cluster = aws.ecs.Cluster(
        "cluster", name=project, settings=[{"name": "containerInsights", "value": "enabled"}], tags=tags
    )
    return cluster, log_group


def create_service(
    repository: aws.ecr.Repository, execution_role: aws.iam.Role,
    task_role: aws.iam.Role, cluster: aws.ecs.Cluster, target_group: aws.lb.TargetGroup,
    service_security_group: aws.ec2.SecurityGroup, private_subnets: list[aws.ec2.Subnet],
    https_listener: aws.lb.Listener, execution_policy: aws.iam.RolePolicyAttachment,
    log_group: aws.cloudwatch.LogGroup, public_associations: list[aws.ec2.RouteTableAssociation],
    private_associations: list[aws.ec2.RouteTableAssociation],
):
    container_definitions = repository.repository_url.apply(lambda url: json.dumps([{
        "name": "app",
        "image": f"{url}:{image_tag}",
        "essential": True,
        "portMappings": [{"containerPort": 8443, "protocol": "tcp"}],
        "environment": [
            {"name": "PORT", "value": "8443"},
            {"name": "ENABLE_TLS", "value": "true"},
        ],
        "healthCheck": {
            "command": ["CMD", "python", "-m", "app.healthcheck"],
            "interval": 30,
            "timeout": 5,
            "retries": 3,
            "startPeriod": 15,
        },
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/{project}",
                "awslogs-region": region,
                "awslogs-stream-prefix": "app",
            },
        },
    }]))
    task_definition = aws.ecs.TaskDefinition(
        "task-definition",
        family=project,
        requires_compatibilities=["FARGATE"],
        network_mode="awsvpc",
        cpu="256",
        memory="512",
        execution_role_arn=execution_role.arn,
        task_role_arn=task_role.arn,
        container_definitions=container_definitions,
        tags=tags,
    )
    service = aws.ecs.Service(
        "service",
        name=project,
        cluster=cluster.arn,
        task_definition=task_definition.arn,
        desired_count=2,
        launch_type="FARGATE",
        deployment_minimum_healthy_percent=100,
        deployment_maximum_percent=200,
        health_check_grace_period_seconds=30,
        deployment_circuit_breaker={"enable": True, "rollback": True},
        network_configuration={
            "subnets": [subnet.id for subnet in private_subnets],
            "security_groups": [service_security_group.id],
            "assign_public_ip": False,
        },
        load_balancers=[{"target_group_arn": target_group.arn, "container_name": "app", "container_port": 8443}],
        tags=tags,
        opts=pulumi.ResourceOptions(
            depends_on=[https_listener, execution_policy, log_group, *public_associations, *private_associations],
            ignore_changes=["taskDefinition", "desiredCount"],
        ),
    )
    scaling_target = aws.appautoscaling.Target(
        "scaling-target",
        max_capacity=4,
        min_capacity=2,
        resource_id=pulumi.Output.concat("service/", cluster.name, "/", service.name),
        scalable_dimension="ecs:service:DesiredCount",
        service_namespace="ecs",
    )
    aws.appautoscaling.Policy(
        "cpu-scaling",
        name=f"{project}-cpu",
        policy_type="TargetTrackingScaling",
        resource_id=scaling_target.resource_id,
        scalable_dimension=scaling_target.scalable_dimension,
        service_namespace=scaling_target.service_namespace,
        target_tracking_scaling_policy_configuration={
            "target_value": 60,
            "predefined_metric_specification": {"predefined_metric_type": "ECSServiceAverageCPUUtilization"},
        },
    )
    return service


def main():
    (vpc, public_subnets, private_subnets, public_associations, private_associations) = create_network()
    alb_security_group, service_security_group = create_security_groups(vpc)
    repository = create_ecr_repository()
    execution_role, execution_policy, task_role, github_role = create_iam_roles(repository)
    load_balancer, target_group, https_listener = create_load_balancer(vpc, public_subnets, alb_security_group)
    cluster, log_group = create_cluster_and_log_group()
    if deploy_service:
        service = create_service(
            repository, execution_role, task_role, cluster, target_group, service_security_group,
            private_subnets, https_listener, execution_policy, log_group,
            public_associations, private_associations,
        )
        pulumi.export("ecsService", service.name)

    pulumi.export("appUrl", f"https://{domain_name}")
    pulumi.export("albDnsName", load_balancer.dns_name)
    pulumi.export("ecrRepositoryUrl", repository.repository_url)
    pulumi.export("ecsCluster", cluster.name)
    pulumi.export("githubActionsRoleArn", github_role.arn)


if __name__ == "__main__":
    main()
