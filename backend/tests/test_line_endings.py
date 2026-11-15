"""Shell scripts and Dockerfiles run in Linux containers and shells, where a
CRLF line ending breaks them: the image's entrypoint fails with "no such
file or directory" when its shebang line ends in a carriage return.
.gitattributes keeps them LF in every checkout; this checks the files."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# Not part of the repository: dependencies, build output and local data.
SKIPPED_DIRS = {".git", "node_modules", "dist", "data", ".venv", "venv", "__pycache__"}


def _lf_only_files() -> list[Path]:
    found = []
    for pattern in ("*.sh", "Dockerfile"):
        for path in REPO.rglob(pattern):
            if SKIPPED_DIRS.isdisjoint(path.relative_to(REPO).parts):
                found.append(path)
    return found


def test_shell_scripts_and_dockerfiles_have_no_carriage_returns():
    files = _lf_only_files()
    assert REPO / "backend" / "docker-entrypoint.sh" in files
    with_cr = [str(path.relative_to(REPO)) for path in files if b"\r" in path.read_bytes()]
    assert with_cr == []


def test_gitattributes_keeps_them_lf():
    rules = (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "*.sh text eol=lf" in rules
    assert "Dockerfile text eol=lf" in rules
