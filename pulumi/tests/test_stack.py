import asyncio
import json
import runpy
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pulumi
import pulumi_aws as aws
from pulumi.runtime.stack import run_pulumi_func


PROJECT = Path(__file__).resolve().parents[1]


class StackMocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources = {}

    def new_resource(self, args):
        self.resources[args.name] = (args.typ, args.inputs)
        outputs = dict(args.inputs)
        outputs.setdefault("name", args.name)
        outputs["arn"] = f"arn:aws:mock:ap-southeast-2:123456789012:{args.name}"
        outputs["repositoryUrl"] = "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/healthz-app"
        outputs["dnsName"] = "healthz.example.elb.amazonaws.com"
        outputs["zoneId"] = "ZALB"
        outputs["fqdn"] = "validation.example.com"
        outputs["domainValidationOptions"] = [{
            "domainName": "healthz.example.com",
            "resourceRecordName": "_validation.healthz.example.com",
            "resourceRecordType": "CNAME",
            "resourceRecordValue": "_validation.acm-validations.aws",
        }]
        return f"{args.name}-id", outputs

    def call(self, args):
        if args.token == "aws:iam/getOpenIdConnectProvider:getOpenIdConnectProvider":
            return {"arn": "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"}
        if args.token == "aws:index/getAvailabilityZones:getAvailabilityZones":
            return {"names": ["ap-southeast-2a", "ap-southeast-2b"]}
        if args.token == "aws:index/getCallerIdentity:getCallerIdentity":
            return {"accountId": "123456789012"}
        if args.token == "aws:route53/getZone:getZone":
            return {"zoneId": "ZHOSTED"}
        raise AssertionError(f"Unexpected provider lookup: {args.token}")


def check_stack(stage):
    deploy_service = stage != "bootstrap"
    mocks = StackMocks()
    pulumi.runtime.set_mocks(mocks, project="healthz-app", stack="test", preview=False)
    values = {
        "aws:region": "ap-southeast-2",
        "healthz-app:domainName": "healthz.example.com",
        "healthz-app:hostedZoneName": "example.com",
        "healthz-app:githubRepository": "owner/repository",
        "healthz-app:deployService": json.dumps(deploy_service),
    }
    if stage == "existing-oidc":
        values["healthz-app:useExistingGithubOidcProvider"] = "true"
    if stage == "immutable-oidc":
        values["healthz-app:githubOidcSubject"] = "repo:owner@123/repository@456:ref:refs/heads/main"
    if deploy_service and stage != "missing-tag":
        values["healthz-app:imageTag"] = "test-sha"
    pulumi.runtime.set_all_config(values)
    with (
        patch.object(aws, "get_availability_zones", return_value=SimpleNamespace(
            names=["ap-southeast-2a", "ap-southeast-2b"]
        )),
        patch.object(aws, "get_caller_identity", return_value=SimpleNamespace(account_id="123456789012")),
        patch.object(aws.route53, "get_zone", return_value=SimpleNamespace(zone_id="ZHOSTED")),
    ):
        try:
            asyncio.get_event_loop().run_until_complete(
                run_pulumi_func(lambda: runpy.run_path(str(PROJECT / "__main__.py"), run_name="__main__"))
            )
        except ValueError as exc:
            if stage == "missing-tag" and "Set imageTag" in str(exc):
                return
            raise

    if stage == "missing-tag":
        raise AssertionError("A service deployment without an image tag must fail.")

    resources = mocks.resources
    assert resources["private-routes"][1]["routes"][0]["natGatewayId"] == "nat-id"
    assert resources["public-routes"][1]["routes"][0]["gatewayId"] == "internet-gateway-id"
    assert resources["target-group"][1]["targetType"] == "ip"
    assert resources["target-group"][1]["healthCheck"]["path"] == "/healthz"
    assert ("github-oidc" in resources) == (stage != "existing-oidc")
    trust = json.loads(resources["github-role"][1]["assumeRolePolicy"])
    subject = trust["Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"]
    assert subject == (
        "repo:owner@123/repository@456:ref:refs/heads/main"
        if stage == "immutable-oidc"
        else "repo:owner/repository:ref:refs/heads/main"
    )
    assert ("service" in resources) == deploy_service
    if deploy_service:
        service = resources["service"][1]
        assert service["networkConfiguration"]["assignPublicIp"] is False
        assert service["loadBalancers"][0]["containerPort"] == 8443
        container = json.loads(resources["task-definition"][1]["containerDefinitions"])[0]
        assert container["image"].endswith(":test-sha")
        assert container["healthCheck"]["command"] == ["CMD", "python", "-m", "app.healthcheck"]
        assert {item["name"]: item["value"] for item in container["environment"]} == {
            "PORT": "8443", "ENABLE_TLS": "true"
        }


class StackTests(unittest.TestCase):
    def test_bootstrap_and_deployed_stack(self):
        for stage in ("bootstrap", "service", "existing-oidc", "immutable-oidc", "missing-tag"):
            with self.subTest(stage=stage):
                result = subprocess.run(
                    [sys.executable, __file__, stage], capture_output=True, text=True, check=False, timeout=30
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in (
        "bootstrap", "service", "existing-oidc", "immutable-oidc", "missing-tag"
    ):
        check_stack(sys.argv[1])
    else:
        unittest.main()
