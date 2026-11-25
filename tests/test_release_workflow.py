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

    script = workflow_script("cd.yml", "deploy", "Verify release belongs to main")
    for version, sha, valid in (
        ("v1.0.0", main_sha, True),
        ("v1.0.0", other_sha, False),
        ("v2.0.0", other_sha, False),
    ):
        result = bash_script(script, work, RELEASE_VERSION=version, SHA=sha)
        assert (result.returncode == 0) == valid, result.stdout + result.stderr
