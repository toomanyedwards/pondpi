import subprocess
from pathlib import Path

from commit_sha import read_commit_sha

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_reads_sha_from_commit_sha_file(tmp_path):
    (tmp_path / "COMMIT_SHA").write_text("abc123\n")
    assert read_commit_sha(tmp_path) == "abc123"


def test_falls_back_to_git_rev_parse_when_no_file():
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()

    assert read_commit_sha(REPO_ROOT) == expected


def test_returns_none_when_no_file_and_not_a_git_repo(tmp_path):
    assert read_commit_sha(tmp_path) is None
