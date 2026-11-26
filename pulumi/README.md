# Pulumi infrastructure

This Pulumi Python project creates a VPC
with public and private subnets, a NAT gateway, ECR, IAM roles, an HTTPS load
balancer, DNS, CloudWatch logs, an ECS Fargate service and CPU autoscaling.
GitHub Actions uses OIDC to push images and deploy the service.

## Project structure

- `Pulumi.yaml`: project and Python runtime settings.
- `Pulumi.template.yaml`: example stack configuration, following the reference
  project's template style. Replace example values using `pulumi config set`;
  do not overwrite a stack file with this template.
- `Pulumi.dev.yaml`: generated when configuring the `dev` stack.
- `__main__.py`: resource-creation functions and stack outputs.
- `requirements.txt`: Pulumi Python dependencies.
- `tests/`: mocked infrastructure tests.

## Before deploying

- This project is for a **fresh stack**, not importing the manually created
  infrastructure. Do not run `pulumi up` against the existing manual setup:
  named resources and DNS records can conflict or be duplicated. Remove the
  manual stack first, with appropriate backups and permission, or use a
  separate account and domain. Removing it causes downtime.
- Keep your registered domain and its **public Route 53 hosted zone**. The
  domain must use that zone's name servers. Choose a domain name and ACM
  validation records that do not already conflict with existing DNS records;
  inspect any existing records before removing them, especially validation
  records used by other certificates.
- AWS credentials need permission to create the resources, including IAM and
  Route 53. Check `aws sts get-caller-identity` before applying. Your
  previously reviewed regional SCP does not restrict requests in
  `ap-southeast-2`; IAM permissions and other policies can still apply.
- Install the Pulumi CLI, Python 3.12, AWS CLI and Docker Desktop.
- Use an existing S3 bucket for Pulumi state, created separately before the
  stack. Enable S3 versioning, block public access, and use server-side
  encryption. Restrict access to the state bucket: Pulumi state can contain
  sensitive infrastructure information. The bucket must not be managed by the
  stack whose state it stores.
- The NAT gateway, public IP, load balancer, Fargate tasks and other resources
  incur AWS charges. S3 state storage and requests can also incur charges.
  Review `pulumi preview` carefully before each update.

## 1. Configure and provision infrastructure

From PowerShell in this directory, replace `<existing-state-bucket>` with the
name of your private state bucket. The AWS credentials must allow access to it:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
pulumi login 's3://<existing-state-bucket>?region=ap-southeast-2'
pulumi stack init dev
pulumi config set aws:region ap-southeast-2
pulumi config set domainName healthz.example.com
pulumi config set hostedZoneName example.com
pulumi config set githubRepository owner/repository
```

For an S3 backend, Pulumi normally prompts for a secrets passphrase when the
stack is created. Store it securely; losing it can prevent access to encrypted
stack configuration. Never commit the passphrase. Use the same backend when
you return to the stack. Switching backends does not migrate state automatically.
See [Pulumi's DIY backend documentation](https://www.pulumi.com/docs/iac/operations/stack-management/using-a-diy-backend/).

Replace the example domain, zone and GitHub repository with your own values.
The DNS name must be inside the existing public hosted zone.
The `pulumi config set` commands write the stack's configuration to
`Pulumi.dev.yaml`. `Pulumi.template.yaml` shows the available values but is not
loaded automatically. Generated stack configuration files are ignored by Git;
share any required settings securely and never commit the secrets passphrase.

For repositories using GitHub's immutable OIDC subjects, including repositories
created after July 15, 2026, set the exact main-branch subject before deploying:

```powershell
pulumi config set githubOidcSubject 'repo:owner@OWNER_ID/repository@REPOSITORY_ID:ref:refs/heads/main'
```

Replace the names and IDs with your repository's values. You can find the
repository and owner IDs at `https://api.github.com/repos/owner/repository`
(`id` and `owner.id`, respectively). The default, when this setting is absent,
is the legacy name-only subject. See the [GitHub OIDC documentation](https://docs.github.com/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services).

If the AWS account already has the GitHub OIDC provider at
`https://token.actions.githubusercontent.com`, set the following to reuse it;
otherwise this project creates one:

```powershell
pulumi config set useExistingGithubOidcProvider true
```

By default, the first update creates infrastructure and the ECR repository but
**not** the ECS service. This breaks the dependency cycle between creating the
repository and deploying an image. It also creates chargeable infrastructure
before the application is running:

```powershell
pulumi preview
pulumi up
```

ACM DNS validation needs the public DNS delegation to work; it can take time.
Do not continue until the update succeeds. If the stack was partially created,
inspect the error and run `pulumi preview` before retrying.

## 2. Configure the existing GitHub Actions workflows

Set these GitHub repository Actions **variables** using the stack outputs:

| Variable | Value |
|---|---|
| `AWS_REGION` | `ap-southeast-2` |
| `AWS_ROLE_ARN` | `pulumi stack output githubActionsRoleArn` |
| `ECR_REPOSITORY` | `healthz-app`, or your configured project name |
| `ECS_CLUSTER` | `pulumi stack output ecsCluster` |
| `ECS_SERVICE` | `healthz-app`, or your configured project name |
| `APP_URL` | `pulumi stack output appUrl` |
| `BOOTSTRAP_MODE` | `true` for the first image only; remove it or set it to `false` after the service is created |

## 3. Build and push an image with CI/CD

Set `main` as the GitHub repository's default branch, set `BOOTSTRAP_MODE` to
`true`, and push the reviewed changes. CI checks the commit; CD builds, scans
and pushes the image to ECR as `sha-<full-commit-SHA>`. If the service does not
yet exist, CD reports the bootstrap state and stops after the image push.

## 4. Start the service

Use the full commit SHA from the successful CI run to configure the image tag:

```powershell
pulumi config set imageTag sha-<full-commit-SHA>
pulumi config set deployService true
pulumi preview
pulumi up
```

Replace the placeholder with the actual tag. Wait for ECS tasks and the ALB
target group to become healthy. Test the HTTPS URL returned by
`pulumi stack output appUrl`, followed by `/healthz` and `/readyz`. Test that
the HTTP URL redirects to HTTPS.

Remove the `BOOTSTRAP_MODE` GitHub variable or set it to `false` after the
service exists, so an unexpectedly missing service causes a deployment failure.
Rerun the bootstrap CD run to smoke-test the service and create its Git release
tag; the tested commit must still be the tip of `main`.
Successful pushes to `main` trigger deployment automatically. Stale runs are
rejected if `main` has moved before verification, and newer runs can replace
pending runs. After a successful smoke test, CD creates a Git tag with the next
patch version. Allow its Actions token to create release tags in repository
settings and any tag rulesets. `vMAJOR.MINOR.PATCH` tags can also be pushed
manually to deploy a named release. CD manages the ECS
task definition, while autoscaling manages desired count; Pulumi ignores changes
to those two service properties. **Run `pulumi refresh` before later Pulumi
updates**, then preview the changes, so Pulumi state reflects deployments made
outside Pulumi. Review changes carefully before applying.

The bootstrap task definition does not set `APP_VERSION`: CD bakes the version
into the image. Later deployments set `APP_VERSION` and `PREVIOUS_APP_VERSION`
to the corresponding deployment versions. Do not retain a manually configured
fixed version such as `APP_VERSION=0.1.0`.
Infrastructure updates are not currently automated by the GitHub workflows;
Pulumi commands above are run manually.

## Staging stack

The optional `staging` stack uses project name `healthz-staging` and domain
`staging.anusha-devops.com`. It reuses the account's GitHub OIDC provider but
creates its own networking, ECR repository, IAM roles, load balancer and ECS
service. It shares the AWS account and state backend, so this is not account
isolation. These resources incur charges, including before the service starts.
Use `--stack staging` on every staging Pulumi command to avoid targeting `dev`.

Once the initial staging infrastructure has been created and the staging
workflow is merged into `main`:

1. Set the GitHub Actions repository variable `STAGING_ROLE_ARN` to the output
   of `pulumi stack output githubActionsRoleArn --stack staging`.
2. In GitHub Actions, run **Staging deployment** on `main` with mode
   `bootstrap`. It scans and pushes an image to the staging ECR repository.
   The job summary reports the image tag. Bootstrap fails if the staging
   service already exists.
3. Configure and preview the staging service, replacing the placeholder with
   that tag. Review the preview before applying:

   ```powershell
   pulumi config set --stack staging imageTag <staging-image-tag>
   pulumi config set --stack staging deployService true
   pulumi preview --stack staging
   pulumi up --stack staging
   ```

4. Wait for staging tasks to become healthy. Run **Staging deployment** again
   on `main` with mode `deploy`. It builds a new staging image, performs a
   rolling update and verifies `/healthz`. If deployment or validation fails,
   it attempts to restore the previous staging task definition.

Staging image and application versions use the workflow run number, for
example `staging-v1.0.4`. Reruns add an attempt suffix; failed or other runs can
leave gaps. Existing `staging-<run-id>-<attempt>` versions remain supported.
After two successful deployments, both current and previous versions use the
new format.

To test automatic rollback, run the staging workflow from `main` in
`rollback-test` mode. It deploys and verifies a new image, intentionally fails
the health step, then restores the previous task definition and checks the
restored version. The job is expected to be **red**; check that the **Verify
intentional rollback** step succeeded and the job summary confirms verification.
If either is missing or fails, investigate before treating the test as passed.
This test affects staging and may temporarily interrupt it.

The workflow validates the staging-specific IAM role and targets only
`healthz-staging`; it does not deploy to the production service. To clean up, review `pulumi preview --stack staging
--destroy` before `pulumi destroy --stack staging`; image removal may be needed
before the staging ECR repository can be destroyed. Never destroy the `dev`
stack as part of staging cleanup.

The ECR lifecycle policy retains tagged images so that active and rollback
images are not automatically expired. Untagged images older than 30 days are
eligible for expiry. Tagged images accrue storage charges; review and remove
obsolete images deliberately only after checking deployment and rollback needs.
The lifecycle policy is deleted before replacement because each repository has
only one policy. During replacement there is briefly no lifecycle policy; if
creation fails, review the error and retry before relying on image expiry.

## Tests and cleanup

Run the mocked resource tests without AWS access:

```powershell
.\.venv\Scripts\python.exe tests\test_stack.py
```

To remove a stack, review `pulumi preview --destroy` first, then use
`pulumi destroy`. Destruction causes downtime and deletes the resources Pulumi
owns. The ECR repository is not force-deleted: if it contains images, destroy
can stop until those images are removed deliberately. The existing hosted zone
and registered domain are not managed or deleted by this project. Check AWS
for any remaining billable resources after cleanup.
