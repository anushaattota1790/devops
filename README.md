# Healthz service – DevOps assessment

A small FastAPI service containerised and deployed to **AWS ECS Fargate** behind an **ALB with TLS**, provisioned by **Pulumi** and delivered by **GitHub Actions**.

```
git push origin main ─► CI ─► CD (build, scan, deploy, smoke test, create release tag)
git push origin v1.0.0 ─► CI ─► CD (verify release tag, build and scan, push, deploy, smoke test, rollback)

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

The version defaults to `dev` locally. A successful `main` deployment displays an automatically allocated release version such as `v1.0.1`; after the smoke test, CD creates a Git tag with that name pointing to the deployed commit. Manually pushed release tags display their tag. The full SHA is recorded in the image label and used in the ECR tag for `main` deployments. The deployment workflow records the version of the previous task definition as `PREVIOUS_APP_VERSION`; older deployments can report `main-<short-SHA>` or a full commit SHA. It is `null` when unavailable. During a rolling deployment, different tasks can temporarily report different versions; rollback restores the old task definition and its recorded value.

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
   | `BOOTSTRAP_MODE` | `true` only until the initial ECS service has been created |

3. Ensure `main` is the repository's default branch. Push the reviewed changes to `main`. CI runs the tests and scans; CD builds, scans and pushes an image tagged `sha-<full-commit-SHA>`. With `BOOTSTRAP_MODE=true`, CD reports that the ECS service must be created and stops without deploying when it does not yet exist.

4. **Create the service** using the image CD just pushed, from the `pulumi` directory:
   ```bash
   pulumi config set imageTag sha-<full-commit-SHA>
   pulumi config set deployService true
   pulumi preview
   pulumi up
   ```
   Replace the placeholder with the actual SHA. The service starts using the existing ALB, certificate and Route53 record. Set the GitHub variable `BOOTSTRAP_MODE` to `false` or remove it after the service is created. Rerun the bootstrap CD run to smoke-test the service and publish its release tag; ensure `main` has not moved in the meantime.

5. From now on, successful `main` pushes trigger deployment automatically. A run is rejected if `main` has moved before its commit is verified; GitHub may also replace a pending deployment when newer runs are queued. CD increments the patch component of the highest existing `vMAJOR.MINOR.PATCH` Git tag, or starts at `v1.0.0` if none exists. Major or minor versions can be advanced by manually pushing a new release tag on a commit reachable from `main`. Restrict who can create or move release tags using GitHub rulesets, but allow the Actions token to create the automatic tags.

> If the account already has the GitHub OIDC provider, set `pulumi config set useExistingGithubOidcProvider true` before provisioning.
> Review `pulumi preview --destroy` before `pulumi destroy`. The NAT gateway and ALB cost money while running.

## Testing the deployment

```bash
curl -i https://<domain>/healthz            # 200 with current and previous versions
curl -i http://<domain>/healthz             # 301 to HTTPS
git tag --points-at HEAD                   # shows the automatically created release tag
```

To verify automatic deployment, merge to `main` and watch CD deploy, smoke-test `/healthz`, and create the matching release tag. The image revision label records the deployed commit. A manually pushed release tag also deploys and displays that tag.

## CI/CD behaviour

**CI** (`ci.yml`, PRs, `main`, and release tags)
1. pytest unit tests
2. Pulumi mocked infrastructure tests
3. bandit (SAST) and pip-audit (dependency CVEs)
4. Trivy filesystem scan for secrets and supported IaC misconfigurations (`.trivyignore` documents accepted findings). Trivy does not scan Pulumi Python as IaC.
5. Docker build, then Trivy image scan (fails on fixable HIGH/CRITICAL)
6. For `main` pushes and `vMAJOR.MINOR.PATCH` tags, upload deployment metadata after all checks pass. CI does not receive AWS credentials.

**CD** (`cd.yml`, triggered after CI succeeds)
1. Validate deployment metadata; `main` deployments must match the latest `main` commit, while release tags must point to a tested commit reachable from `main`.
2. Allocate the next patch version for `main`, or use the manually pushed release tag. Rebuild and scan the image, then push it with an immutable `sha-<full-SHA>` or release tag. The `org.opencontainers.image.revision` label records the commit SHA. On retry, the existing image's revision and version are verified and the image is scanned before use.
3. Record the current task definition, then deploy a rolling update (100% min healthy, 200% max) and wait for stability. The ECS deployment **circuit breaker** also rolls back if new tasks never become healthy.
4. Smoke test `/healthz` for the deployment and previous version. After a successful `main` smoke test, create a Git tag pointing to the deployed commit. If deployment, validation, or tag creation fails after an update, roll back to the recorded task definition. Tag conflicts fail rather than move existing tags; a failed network response can leave the tag published, so check GitHub before retrying.
5. Deployments are serialised (`concurrency`) so two runs cannot race. GitHub allows one running and one pending run in the group; a newer pending run replaces an older one.

CD needs GitHub Actions `contents: write` permission to create tags. Configure repository Actions settings and tag rulesets to allow this. A tag pushed with `GITHUB_TOKEN` does not trigger a second CI/CD run; tags pushed by a user do. The bootstrap image is not tagged in Git until the service is created and the CD run is rerun successfully.

## Security

- **Least privilege**: the app's task role has *no* permissions; the execution role only has the AWS-managed ECS execution policy; the GitHub role can only push to this ECR repo, update this one service and pass these two roles, and only from `main` of this repo (OIDC `sub` condition)
- **No hardcoded secrets**: no long-lived AWS keys (OIDC), nothing sensitive in code or image, `.trivyignore`/secret scanning in CI
- **TLS**: HTTP→HTTPS redirect, ACM certificate, TLS 1.3/1.2 policy on the ALB, and the ALB re-encrypts to the container over HTTPS. The container generates a throwaway self-signed cert at start-up; the ALB does not authenticate target certificates, so backend encryption does not verify target identity.
- **Network**: tasks run in private subnets with no public IP; the task security group only accepts traffic from the ALB security group
- **Container**: slim version-tagged base (not digest-pinned), multi-stage build, non-root user, no build tools in the final image, ECR scan-on-push

## Branching and merging strategy (discussion notes)

- **Trunk-based / GitHub Flow**: use short-lived branches and pull requests into `main`. Branch protection is recommended but must be configured separately in GitHub; for a sole contributor, requiring an approving reviewer may block merges.
- Short-lived `feature/*`, `fix/*` branches off `main`; small PRs; squash merge
- CI runs on every PR and push to `main`; successful pushes to `main` trigger deployment. Pushing a release tag also triggers deployment.
- **Versioning**: `main` deployments create `v1.2.3`-style Git tags and use the full SHA in their immutable ECR tag; manually tagged releases use the Git tag as their ECR tag. Image labels record the full SHA.
- **Hotfix**: branch from `main`, fast-track PR; rollback restores the previous task definition
- **Release safety**: feature flags for incomplete work; environments (dev → staging → prod) with approval gates can be added by promoting the same image digest

## Trade-offs and next steps

- ECS Fargate chosen over EKS for lower operational overhead; EKS would be the choice for a larger platform or portability needs
- Single NAT gateway (cost); use one per AZ for production. VPC endpoints for ECR/S3/Logs would remove NAT dependency
- Tagged ECR images are retained to protect running and rollback images; review storage usage and remove obsolete images deliberately.
- Validate the rollback path with a deliberately failing deployment in a separate test environment before claiming it has been proven end to end.
- Next steps: blue/green or canary via CodeDeploy, WAF on the ALB, ALB access logs, alarms and dashboards (latency, 5xx, unhealthy hosts), multiple environments, `readonlyRootFilesystem`, image signing (cosign), Pulumi preview on PR with a manual apply gate
