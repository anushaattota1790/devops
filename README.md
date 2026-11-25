# Healthz service – DevOps assessment

A small FastAPI service containerised and deployed to **AWS ECS Fargate** behind an **ALB with TLS**, provisioned by **Pulumi** and delivered by **GitHub Actions**.

```
git push origin main ─► CI (test, SAST, dep scan, IaC/secret scan, build, image scan)
git push v1.0.0 ─► CI ─► CD (verify tag, build and scan, push to ECR, deploy, smoke test, rollback)

Route53 ─► ALB :443 (ACM cert, TLS 1.3 policy) ─► ECS Fargate tasks (private subnets, HTTPS :8443)
```

## Repository layout

| Path | Purpose |
|---|---|
| `app/` | FastAPI app (`/healthz`), TLS bootstrap, container healthcheck |
| `tests/` | pytest unit tests |
| `Dockerfile` | Multi-stage, slim base, non-root (uid 10001), env-var config, `HEALTHCHECK` |
| `pulumi/` | VPC, ALB, ACM, Route53, ECR, ECS Fargate, IAM and GitHub OIDC role; see its [setup guide](pulumi/README.md) |
| `.github/workflows/ci.yml` | Test, scan and build |
| `.github/workflows/cd.yml` | Verify release, build, scan, push, deploy and roll back |

## The application

`GET /healthz` → `{"status":"ok","version":"v1.0.0","previous_version":"v0.9.0"}`

`GET /readyz` → `200 {"status":"ready"}` after application startup, or `503 {"status":"not ready"}` before startup or during shutdown. This checks the application lifecycle only; it does not check external dependencies.

The version defaults to `dev` locally and the release tag is baked into the image (`--build-arg APP_VERSION=v1.0.0`). The deployment workflow sets `PREVIOUS_APP_VERSION` from the previous ECS task definition's image tag. The previous version is `null` when it is not supplied or the previous image has an unrecognized tag; older deployments can report a commit SHA. This is the version deployed before this task was created, not a deployment history; a rollback restores the old task's recorded value. During a rolling deployment, different tasks can temporarily report different versions.

Config (env vars): `PORT` (default 8080), `ENABLE_TLS` (default false; true in ECS), `APP_VERSION`, `PREVIOUS_APP_VERSION`, `HOST`.

Run locally:

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest
python -m app.server                              # http://localhost:8080/healthz

docker build --build-arg APP_VERSION=local -t healthz-app .
docker run --rm -p 8080:8080 healthz-app
```

## Prerequisites

- AWS account + credentials for Pulumi (CI/CD itself uses OIDC, no keys)
- A **Route53 public hosted zone** for a domain you control
- Pulumi CLI, Python 3.12, a state backend and a GitHub repo for this code

## Deploy from scratch

1. Follow the [Pulumi setup guide](pulumi/README.md) to configure state and provision the initial infrastructure. The first update creates ECR and the GitHub role, but not the ECS service.

2. **Set GitHub repository variables** (Settings → Secrets and variables → Actions → *Variables*). No secrets are needed.

   | Variable | Value |
   |---|---|
   | `AWS_REGION` | `ap-southeast-2` |
   | `AWS_ROLE_ARN` | `pulumi stack output githubActionsRoleArn` |
   | `ECR_REPOSITORY` | `healthz-app` |
   | `ECS_CLUSTER` / `ECS_SERVICE` | `healthz-app` |
   | `APP_URL` | `https://<domain_name>` |

3. Ensure `main` is the repository's default branch. Push the reviewed changes to `main`, then create and push a release tag on that commit:
   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```
   CI tests the tag; CD verifies it points to a commit on `main`, builds and scans the release image, and pushes `healthz-app:v1.0.0` to ECR. On the first release, CD fails after the push because the ECS service does not exist yet.

4. **Create the service** using the image CI just pushed, from the `pulumi` directory:
   ```bash
   pulumi config set imageTag v1.0.0
   pulumi config set deployService true
   pulumi preview
   pulumi up
   ```
   The service starts using the existing ALB, certificate and Route53 record.

5. From now on, pushes to `main` run CI but do not deploy. Push a new `vMAJOR.MINOR.PATCH` tag on a commit reachable from `main` to release it. Restrict who can create or move release tags using GitHub rulesets.

> If the account already has the GitHub OIDC provider, set `pulumi config set useExistingGithubOidcProvider true` before provisioning.
> Review `pulumi preview --destroy` before `pulumi destroy`. The NAT gateway and ALB cost money while running.

## Testing the deployment

```bash
curl -i https://<domain>/healthz            # 200 with current and previous versions
curl -i http://<domain>/healthz             # 301 to HTTPS
git tag --points-at HEAD                   # release tag should equal "version" after CD finishes
```

To see the pipeline prove the latest release is live: merge to `main`, push a new release tag, and watch the CD smoke test poll `/healthz` until `version` equals that tag.

## CI/CD behaviour

**CI** (`ci.yml`, PRs, `main`, and release tags)
1. pytest unit tests
2. bandit (SAST) and pip-audit (dependency CVEs)
3. Trivy filesystem scan for secrets and supported IaC misconfigurations (`.trivyignore` documents accepted findings)
4. Docker build, then Trivy image scan (fails on fixable HIGH/CRITICAL)
5. For a `vMAJOR.MINOR.PATCH` tag, upload release metadata after all checks pass. Tag pushes themselves do not receive AWS credentials.

**CD** (`cd.yml`, triggered after CI succeeds)
1. Ignore runs without release metadata. Validate the version and verify that the tag points to the tested commit on `main`.
2. Rebuild and scan the release image, then push it with the immutable release tag. The image's `org.opencontainers.image.revision` label records the commit SHA. For a retry, an existing image is verified and scanned before use.
3. Record the current task definition, then deploy a rolling update (100% min healthy, 200% max) and wait for stability. The ECS deployment **circuit breaker** also rolls back if new tasks never become healthy.
4. Smoke test `/healthz` for the release and previous version. If deployment or validation fails, roll back to the recorded task definition.
5. Deployments are serialised (`concurrency`) so two runs cannot race.

## Security

- **Least privilege**: the app's task role has *no* permissions; the execution role only has the AWS-managed ECS execution policy; the GitHub role can only push to this ECR repo, update this one service and pass these two roles, and only from `main` of this repo (OIDC `sub` condition)
- **No hardcoded secrets**: no long-lived AWS keys (OIDC), nothing sensitive in code or image, `.trivyignore`/secret scanning in CI
- **TLS everywhere**: HTTP→HTTPS redirect, ACM certificate, TLS 1.3/1.2 policy on the ALB, and the ALB re-encrypts to the container over HTTPS. The container generates a throwaway self-signed cert at start-up (ALB does not validate target certs), so no private key is stored in the image or repo
- **Network**: tasks run in private subnets with no public IP; the task security group only accepts traffic from the ALB security group
- **Container**: slim pinned base, multi-stage build, non-root user, no build tools in the final image, ECR scan-on-push

## Branching and merging strategy (discussion notes)

- **Trunk-based / GitHub Flow**: `main` is always deployable and protected (no direct pushes, PR required, CI status checks required, at least one review, linear history)
- Short-lived `feature/*`, `fix/*` branches off `main`; small PRs; squash merge
- CI runs on every PR and push to `main` (tests + scans + image build/scan, no push). Pushing a release tag triggers the deployment.
- **Versioning**: image tagged with the immutable release version (`v1.2.0`); the image label records the git SHA. Conventional Commits can automate the changelog and version bump
- **Hotfix**: branch from `main`, fast-track PR; rollback restores the previous task definition
- **Release safety**: feature flags for incomplete work; environments (dev → staging → prod) with approval gates can be added by promoting the same image digest

## Trade-offs and next steps

- ECS Fargate chosen over EKS for lower operational overhead; EKS would be the choice for a larger platform or portability needs
- Single NAT gateway (cost); use one per AZ for production. VPC endpoints for ECR/S3/Logs would remove NAT dependency
- Next steps: blue/green or canary via CodeDeploy, WAF on the ALB, ALB access logs, alarms and dashboards (latency, 5xx, unhealthy hosts), multiple environments, `readonlyRootFilesystem`, image signing (cosign), Pulumi preview on PR with a manual apply gate
