"""scripts/ci/check-sops-staged.sh refuses a *.sops.yaml under k8s/ that was never encrypted, before it is committed
(staged files) and before it is pushed (every version in a range of commits). gitleaks cannot: its plaintext-Secret
rule allows every *.sops.yaml.

Each test makes a scratch git repository and runs the script there. The encrypted file is SOPS-shaped, made up here:
the check reads its shape, never decrypts it.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "check-sops-staged.sh"

PLAIN = """apiVersion: v1
kind: Secret
metadata:
  name: app
  namespace: app
stringData:
  PASSWORD: hunter2
"""
ENCRYPTED = """apiVersion: v1
kind: Secret
metadata:
    name: app
    namespace: app
stringData:
    PASSWORD: ENC[AES256_GCM,data:abc=,iv:def=,tag:ghi=,type:str]
    USER: ENC[AES256_GCM,data:jkl=,iv:mno=,tag:pqr=,type:str]
sops:
    age:
        - recipient: age1example
          enc: |
            -----BEGIN AGE ENCRYPTED FILE-----
            -----END AGE ENCRYPTED FILE-----
    lastmodified: "2026-10-13T00:00:00Z"
    mac: ENC[AES256_GCM,data:stu=,iv:vwx=,tag:yz0=,type:str]
    version: 3.13.3
"""
HALF = ENCRYPTED.replace("USER: ENC[AES256_GCM,data:jkl=,iv:mno=,tag:pqr=,type:str]", "USER: admin")
INLINE = ENCRYPTED.replace("stringData:\n", "stringData: {USER: admin}\nother:\n")


def git(repo, *args):
    identity = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid"}
    identity |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        env={**os.environ, **identity},
        check=True,
        capture_output=True,
    )


def repo_with(tmp_path, files):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README").write_text("x\n")
    git(tmp_path, "add", "README")
    git(tmp_path, "commit", "-q", "-m", "start")
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        git(tmp_path, "add", name)
    return tmp_path


def check(repo, **env):
    result = subprocess.run([str(SCRIPT)], cwd=repo, env={**os.environ, **env}, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def test_an_encrypted_file_passes(tmp_path):
    rc, out = check(repo_with(tmp_path, {"k8s/apps/app/app.sops.yaml": ENCRYPTED}))
    assert rc == 0, out


def test_a_plaintext_file_is_refused(tmp_path):
    rc, out = check(repo_with(tmp_path, {"k8s/apps/app/app.sops.yaml": PLAIN}))
    assert rc != 0 and "app.sops.yaml: PASSWORD is not encrypted" in out and "no sops: metadata" in out, out
    assert "sops --encrypt --in-place" in out


def test_one_plaintext_value_among_encrypted_ones_is_refused(tmp_path):
    rc, out = check(repo_with(tmp_path, {"k8s/apps/app/app.sops.yml": HALF}))
    assert rc != 0 and "USER is not encrypted" in out and "PASSWORD" not in out, out


def test_inline_values_are_refused(tmp_path):
    rc, out = check(repo_with(tmp_path, {"k8s/apps/app/app.sops.yaml": INLINE}))
    assert rc != 0 and "stringData is written inline" in out, out


def test_other_files_are_not_its_business(tmp_path):
    rc, out = check(repo_with(tmp_path, {"k8s/apps/app/notes.yaml": PLAIN, "elsewhere/app.sops.yaml": PLAIN}))
    assert rc == 0, out


def test_a_path_git_would_quote_is_checked(tmp_path):
    """A non-ASCII path, which git quotes in a plain listing, is still found."""
    rc, out = check(repo_with(tmp_path, {"k8s/apps/média/creds.sops.yaml": PLAIN}))
    assert rc != 0 and "creds.sops.yaml: PASSWORD is not encrypted" in out, out


def test_a_range_git_cannot_read_fails(tmp_path):
    rc, out = check(repo_with(tmp_path, {}), RANGE="nothing..HEAD")
    assert rc != 0, out


def test_a_file_changed_in_a_merge_is_checked(tmp_path):
    """A plaintext value pasted while resolving a merge is in the merge commit alone."""
    repo = repo_with(tmp_path, {"k8s/apps/app/app.sops.yaml": ENCRYPTED})
    git(repo, "commit", "-q", "-m", "encrypted")
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "README").write_text("side\n")
    git(repo, "commit", "-q", "-am", "side")
    git(repo, "checkout", "-q", "main")
    (repo / "README").write_text("main\n")
    git(repo, "commit", "-q", "-am", "main")
    git(repo, "merge", "-q", "--no-commit", "-X", "ours", "side")
    (repo / "k8s/apps/app/app.sops.yaml").write_text(HALF)
    git(repo, "commit", "-q", "-am", "merge, with a value pasted in")
    rc, out = check(repo, RANGE="HEAD~1..HEAD")
    assert rc != 0 and "USER is not encrypted" in out, out


def test_every_version_in_a_range_is_checked(tmp_path):
    """A plaintext version committed, then encrypted in the next commit, is still published by the push."""
    repo = repo_with(tmp_path, {"k8s/apps/app/app.sops.yaml": PLAIN})
    git(repo, "commit", "-q", "-m", "plaintext")
    (repo / "k8s/apps/app/app.sops.yaml").write_text(ENCRYPTED)
    git(repo, "commit", "-q", "-am", "encrypted")
    rc, out = check(repo, RANGE="HEAD~2..HEAD")
    assert rc != 0 and "(in " in out and "PASSWORD is not encrypted" in out, out
    rc, out = check(repo, RANGE="HEAD~1..HEAD")
    assert rc == 0, out
