# Healthz service – DevOps assessment

A small FastAPI service containerised and deployed to **AWS ECS Fargate** behind an **ALB with TLS**, provisioned by **Pulumi** and delivered by **GitHub Actions**.

```
git push main ─► CI (test, SAST, dep scan, IaC/secret scan, build, image scan, push to ECR)
                  └─► CD (render task def, rolling deploy, wait stable, smoke test, auto rollback)

Route53 ─► ALB :443 (ACM cert, TLS 1.3 policy) ─► ECS Fargate tasks (private subnets, HTTPS :8443)
```

## Repository layout

| Path | Purpose |
|---|---|
| `app/` | FastAPI app (`/healthz`), TLS bootstrap, container healthcheck |
| `tests/` | pytest unit tests |
| `Dockerfile` | Multi-stage, slim base, non-root (uid 10001), env-var config, `HEALTHCHECK` |
| `pulumi/` | VPC, ALB, ACM, Route53, ECR, ECS Fargate, IAM and GitHub OIDC role; see its [setup guide](pulumi/README.md) |
| `.github/workflows/ci.yml` | Test, scan, build, push |
| `.github/workflows/cd.yml` | Deploy, verify, roll back |

## The application

`GET /healthz` → `{"status":"ok","version":"<git sha>","previous_version":"<previous git sha>"}`

`GET /readyz` → `200 {"status":"ready"}` after application startup, or `503 {"status":"not ready"}` before startup or during shutdown. This checks the application lifecycle only; it does not check external dependencies.

The version defaults to `dev` locally and is baked into the image (`--build-arg APP_VERSION=<sha>`). The deployment workflow sets `PREVIOUS_APP_VERSION` from the previous ECS task definition's image tag. The previous version is `null` when it is not supplied or the previous image is not tagged with a commit SHA, including the initial deployment. This is the version that was deployed before this task was created, not a deployment history; a rollback restores the old task's recorded value. During a rolling deployment, different tasks can temporarily report different versions.

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

3. **Push to `main`** – CI builds and pushes `healthz-app:<sha>` to ECR. CD will fail on this initial run because the ECS service does not exist yet.

4. **Create the service** using the image CI just pushed, from the `pulumi` directory:
   ```bash
   pulumi config set imageTag <sha-from-ci>
   pulumi config set deployService true
   pulumi preview
   pulumi up
   ```
   The service starts using the existing ALB, certificate and Route53 record.

5. From now on every merge to `main` deploys automatically.

> If the account already has the GitHub OIDC provider, set `pulumi config set useExistingGithubOidcProvider true` before provisioning.
> Review `pulumi preview --destroy` before `pulumi destroy`. The NAT gateway and ALB cost money while running.

## Testing the deployment

```bash
curl -i https://<domain>/healthz            # 200 with current and previous versions
curl -i http://<domain>/healthz             # 301 to HTTPS
git rev-parse HEAD                          # should equal "version" after CD finishes
```

To see the pipeline prove the latest code is live: change something, merge to `main`, and watch the CD smoke test poll `/healthz` until `version` equals the new commit SHA.

## CI/CD behaviour

**CI** (`ci.yml`, PRs and `main`)
1. pytest unit tests
2. bandit (SAST) and pip-audit (dependency CVEs)
3. Trivy filesystem scan for secrets and supported IaC misconfigurations (`.trivyignore` documents accepted findings)
4. Docker build, then Trivy image scan (fails on fixable HIGH/CRITICAL)
5. On `main` only: assume AWS role via OIDC, push the image to ECR tagged with the commit SHA (ECR tags are immutable, so a tag always maps to exactly one image)

**CD** (`cd.yml`, runs only after CI succeeds on `main`)
1. Record the current task definition (rollback target)
2. Render a new task definition revision with the new image and deploy a rolling update (100% min healthy, 200% max)
3. Wait for service stability. The ECS deployment **circuit breaker** also rolls back automatically if new tasks never become healthy
4. Smoke test the public URL until `/healthz` returns `ok`, the new SHA, and the previous version
5. If anything fails, the pipeline explicitly rolls the service back to the previous task definition
6. Deploys are serialised (`concurrency`) so two runs cannot race

## Security

- **Least privilege**: the app's task role has *no* permissions; the execution role only has the AWS-managed ECS execution policy; the GitHub role can only push to this ECR repo, update this one service and pass these two roles, and only from `main` of this repo (OIDC `sub` condition)
- **No hardcoded secrets**: no long-lived AWS keys (OIDC), nothing sensitive in code or image, `.trivyignore`/secret scanning in CI
- **TLS everywhere**: HTTP→HTTPS redirect, ACM certificate, TLS 1.3/1.2 policy on the ALB, and the ALB re-encrypts to the container over HTTPS. The container generates a throwaway self-signed cert at start-up (ALB does not validate target certs), so no private key is stored in the image or repo
- **Network**: tasks run in private subnets with no public IP; the task security group only accepts traffic from the ALB security group
- **Container**: slim pinned base, multi-stage build, non-root user, no build tools in the final image, ECR scan-on-push

## Branching and merging strategy (discussion notes)

- **Trunk-based / GitHub Flow**: `main` is always deployable and protected (no direct pushes, PR required, CI status checks required, at least one review, linear history)
- Short-lived `feature/*`, `fix/*` branches off `main`; small PRs; squash merge
- CI runs on every PR (tests + scans + image build/scan, no push). Merge to `main` triggers build, push and deploy
- **Versioning**: image tagged with the git SHA (immutable, traceable); semantic version git tags (`v1.2.0`) mark releases. Conventional Commits can automate the changelog and version bump
- **Hotfix**: branch from `main`, fast-track PR; rollback is just redeploying the previous SHA/task definition
- **Release safety**: feature flags for incomplete work; environments (dev → staging → prod) with approval gates can be added by promoting the same image digest

## Trade-offs and next steps

- ECS Fargate chosen over EKS for lower operational overhead; EKS would be the choice for a larger platform or portability needs
- Single NAT gateway (cost); use one per AZ for production. VPC endpoints for ECR/S3/Logs would remove NAT dependency
- Next steps: blue/green or canary via CodeDeploy, WAF on the ALB, ALB access logs, alarms and dashboards (latency, 5xx, unhealthy hosts), multiple environments, `readonlyRootFilesystem`, image signing (cosign), Pulumi preview on PR with a manual apply gate
