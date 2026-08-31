import subprocess
from pathlib import Path


def read_commit_sha(directory):
    """Returns the commit SHA deployed at `directory`.

    Prefers a COMMIT_SHA file (written by CI at deploy time, since .git is
    excluded from the rsync'd deploy directory) and falls back to
    `git rev-parse HEAD` for local/dev checkouts. Returns None if neither
    is available.
    """
    directory = Path(directory)
    sha_file = directory / "COMMIT_SHA"
    if sha_file.exists():
        return sha_file.read_text().strip()

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
