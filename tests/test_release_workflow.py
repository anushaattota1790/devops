import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def workflow_script(workflow, job, name):
    with (ROOT / ".github" / "workflows" / workflow).open(encoding="utf-8") as file:
        steps = yaml.safe_load(file)["jobs"][job]["steps"]
    return next(step["run"] for step in steps if step.get("name") == name)


def bash_script(script, cwd, **variables):
    bash = shutil.which("bash")
    if os.name == "nt":
        git = shutil.which("git")
        assert git is not None
        bash = str(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    assert bash is not None
    return subprocess.run(
        [bash, "-e", "-c", script],
        cwd=cwd,
        env={**os.environ, **variables},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("version", "valid"),
    [
        ("v1.0.0", True),
        ("v0.9.12", True),
        ("v01.0.0", False),
        ("v1.0", False),
        ("v1.0.0-rc1", False),
        ("v1.0.0/x", False),
    ],
)
def test_release_version_validation(version, valid):
    script = workflow_script("ci.yml", "build", "Validate release version")
    result = bash_script(script, ROOT, IMAGE_VERSION=version)
    assert (result.returncode == 0) == valid, result.stderr


@pytest.mark.parametrize(
    ("ref", "image_version", "expected"),
    [
        ("refs/heads/main", "unused", "main-0123456789ab"),
        ("refs/tags/v1.0.0", "v1.0.0", "v1.0.0"),
        ("refs/pull/1/merge", "unused", "ci-0123456789ab"),
    ],
)
def test_image_version_for_main_release_and_pull_request(tmp_path, ref, image_version, expected):
    script = workflow_script("ci.yml", "build", "Determine image version")
    output = tmp_path / "output"
    result = bash_script(
        script, ROOT, GITHUB_REF=ref, GITHUB_SHA="0123456789abcdef" * 2 + "01234567",
        IMAGE_VERSION=image_version, GITHUB_OUTPUT=str(output),
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8").strip() == f"version={expected}"


@pytest.mark.parametrize(
    ("ref", "version", "valid", "image_tag"),
    [
        ("main", "main-0123456789ab", True, "sha-0123456789abcdef0123456789abcdef01234567"),
        ("v1.0.0", "v1.0.0", True, "v1.0.0"),
        ("main", "v1.0.0", False, ""),
        ("v1.0.0", "main-0123456789ab", False, ""),
        ("main", "main-ffffffffffff", False, ""),
    ],
)
def test_deployment_metadata_matches_ci_ref(tmp_path, ref, version, valid, image_tag):
    (tmp_path / "release-version.txt").write_text(version, encoding="utf-8")
    output = tmp_path / "output"
    result = bash_script(
        workflow_script("cd.yml", "deploy", "Validate deployment version"), ROOT,
        METADATA_DIR=str(tmp_path), GITHUB_OUTPUT=str(output),
        SHA="0123456789abcdef" * 2 + "01234567", TRIGGER_REF=ref,
    )
    assert (result.returncode == 0) == valid, result.stdout + result.stderr
    if valid:
        assert f"image_tag={image_tag}" in output.read_text(encoding="utf-8").splitlines()


def test_release_tag_must_match_tested_commit_on_main(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"

    def git(*args, cwd=tmp_path):
        return subprocess.run(
            ["git", *map(str, args)], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "--bare", remote)
    git("clone", remote, work)
    git("config", "user.email", "test@example.invalid", cwd=work)
    git("config", "user.name", "Test", cwd=work)
    git("checkout", "-b", "main", cwd=work)
    (work / "file").write_text("main", encoding="utf-8")
    git("add", "file", cwd=work)
    git("commit", "-m", "main", cwd=work)
    main_sha = git("rev-parse", "HEAD", cwd=work)
    git("tag", "v1.0.0", cwd=work)
    git("push", "origin", "main", "--tags", cwd=work)
    git("checkout", "-b", "other", cwd=work)
    (work / "file").write_text("other", encoding="utf-8")
    git("commit", "-am", "other", cwd=work)
    other_sha = git("rev-parse", "HEAD", cwd=work)
    git("tag", "v2.0.0", cwd=work)
    git("push", "origin", "v2.0.0", cwd=work)

    script = workflow_script("cd.yml", "deploy", "Verify deployment commit")
    for version, sha, deployment_type, valid in (
        ("v1.0.0", main_sha, "release", True),
        ("v1.0.0", other_sha, "release", False),
        ("v2.0.0", other_sha, "release", False),
        ("main", main_sha, "main", True),
        ("main", other_sha, "main", False),
    ):
        result = bash_script(script, work, RELEASE_VERSION=version, SHA=sha, DEPLOYMENT_TYPE=deployment_type)
        assert (result.returncode == 0) == valid, result.stdout + result.stderr


@pytest.fixture
def release_repository(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"

    def git(*args, cwd=work):
        return subprocess.run(
            ["git", *map(str, args)], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "--bare", remote, cwd=tmp_path)
    git("clone", remote, work, cwd=tmp_path)
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("checkout", "-b", "main")
    (work / "file").write_text("first", encoding="utf-8")
    git("add", "file")
    git("commit", "-m", "first")
    first = git("rev-parse", "HEAD")
    (work / "file").write_text("second", encoding="utf-8")
    git("commit", "-am", "second")
    sha = git("rev-parse", "HEAD")
    git("push", "origin", "main")
    return work, git, first, sha


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ([], "v1.0.0"),
        (["v1.0.9", "v1.1.3", "v2.0.0-rc1"], "v1.1.4"),
    ],
)
def test_main_deployment_selects_next_patch_version(tmp_path, release_repository, tags, expected):
    work, git, _, sha = release_repository
    for tag in tags:
        git("tag", tag)
    git("push", "origin", "--tags")
    output = tmp_path / "output"
    result = bash_script(
        workflow_script("cd.yml", "deploy", "Select deployment version"), work,
        DEPLOYMENT_TYPE="main", REQUESTED_VERSION="unused", IMAGE_EXISTS="false",
        IMAGE="unused", SHA=sha, GITHUB_OUTPUT=str(output),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text(encoding="utf-8").strip() == f"version={expected}"


def test_retry_reuses_image_version_and_rejects_conflicting_tag(tmp_path, release_repository):
    work, git, first, sha = release_repository
    git("tag", "v1.0.0", first)
    git("push", "origin", "--tags")
    script = """
docker() {
    if [[ "$1" == "pull" ]]; then return 0; fi
    if [[ "$*" == *revision* ]]; then echo "$MOCK_REVISION"; else echo "$MOCK_VERSION"; fi
}
""" + workflow_script("cd.yml", "deploy", "Select deployment version")
    output = tmp_path / "output"
    variables = dict(
        DEPLOYMENT_TYPE="main", REQUESTED_VERSION="unused", IMAGE_EXISTS="true",
        IMAGE="example", SHA=sha, GITHUB_OUTPUT=str(output), MOCK_REVISION=sha,
    )
    result = bash_script(script, work, MOCK_VERSION="v1.0.1", **variables)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text(encoding="utf-8").strip() == "version=v1.0.1"
    result = bash_script(script, work, MOCK_VERSION="v1.0.0", **variables)
    assert result.returncode != 0
    assert "already points to another commit" in result.stdout
    variables["MOCK_REVISION"] = first
    result = bash_script(script, work, MOCK_VERSION="v1.0.1", **variables)
    assert result.returncode != 0
    assert "does not match the tested commit" in result.stdout


def test_manual_release_keeps_its_tag_and_checks_commit(tmp_path, release_repository):
    work, git, first, sha = release_repository
    git("tag", "v2.0.0", sha)
    git("push", "origin", "--tags")
    output = tmp_path / "output"
    script = workflow_script("cd.yml", "deploy", "Select deployment version")
    variables = dict(
        DEPLOYMENT_TYPE="release", REQUESTED_VERSION="v2.0.0", IMAGE_EXISTS="false",
        IMAGE="unused", GITHUB_OUTPUT=str(output),
    )
    result = bash_script(script, work, SHA=sha, **variables)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text(encoding="utf-8").strip() == "version=v2.0.0"
    result = bash_script(script, work, SHA=first, **variables)
    assert result.returncode != 0
    assert "no longer points to the tested commit" in result.stdout


def test_publish_release_tag_is_idempotent_and_rejects_conflicts(release_repository):
    work, git, first, sha = release_repository
    git("tag", "v1.0.0", first)
    git("push", "origin", "--tags")
    script = workflow_script("cd.yml", "deploy", "Create release tag")
    for _ in range(2):
        result = bash_script(script, work, RELEASE_VERSION="v1.0.1", SHA=sha)
        assert result.returncode == 0, result.stdout + result.stderr
    assert git("ls-remote", "--tags", "origin", "refs/tags/v1.0.1").split()[0] == sha
    result = bash_script(script, work, RELEASE_VERSION="v1.0.0", SHA=sha)
    assert result.returncode != 0
    assert "already points to another commit" in result.stdout


def test_rollback_restores_previous_task_definition(tmp_path):
    log = tmp_path / "aws-calls"
    script = """
aws() { printf '%s\\n' "$*" >> "$AWS_CALLS"; }
""" + workflow_script("cd.yml", "deploy", "Roll back to previous task definition")
    task_definition = "arn:aws:ecs:ap-southeast-2:123456789012:task-definition/app:1"
    result = bash_script(
        script, ROOT, AWS_CALLS=str(log), CLUSTER="cluster", SERVICE="service",
        TASK_DEFINITION=task_definition,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"ecs update-service --cluster cluster --service service --task-definition {task_definition}",
        "ecs wait services-stable --cluster cluster --services service",
    ]
