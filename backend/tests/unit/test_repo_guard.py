"""Tests for tools/check_repo.py using throw-away git repositories."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

_spec = importlib.util.spec_from_file_location("check_repo", REPO_ROOT / "tools" / "check_repo.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    return tmp_path


def stage(repo: Path, name: str, content: bytes | str) -> None:
    f = repo / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    git(repo, "add", "-f", name)


def problems(repo: Path) -> list[str]:
    return guard.check_staged(repo)


def test_clean_commit_passes(repo):
    stage(repo, "src/app.py", "print('hello')\n")
    stage(repo, "frontend/logo.png", b"\x89PNG\r\n\x1a\n\x00\x00")
    assert problems(repo) == []


@pytest.mark.parametrize(
    "name",
    [
        "x.docx",
        "deep/dir/Template.DOCX",
        "x.pdf",
        "a.db",
        "a.sqlite3",
        "a.db-wal",
        "storage/anything.txt",
        "templates/readme.txt",
        "spike/assets/logo.txt",
        "data/x.txt",
        ".env",
        "backend/.env.production",
        ".private-denylist",
    ],
)
def test_blocked_paths(repo, name):
    stage(repo, name, "x")
    assert problems(repo), name


def test_env_example_is_allowed(repo):
    stage(repo, "backend/.env.example", "THERAPY_PORT=8000\n")
    assert problems(repo) == []


def test_unanchored_look_alike_directories_are_not_blocked(repo):
    stage(repo, "frontend/src/features/templates/index.ts", "export {}\n")
    stage(repo, "backend/app/data/loader.py", "x = 1\n")
    assert problems(repo) == []


def test_unknown_binary_is_blocked_but_frontend_images_allowed(repo):
    stage(repo, "tools/blob.bin", b"\x00\x01\x02")
    stage(repo, "frontend/public/ok.png", b"\x89PNG\x00")
    found = problems(repo)
    assert len(found) == 1 and "blob.bin" in found[0]


@pytest.mark.parametrize(
    "text, blocked",
    [
        ("amka = '15039912345'", True),  # amka-ok (invented number used to test detection)
        ("x 15039912345 y", True),  # amka-ok
        ("amka = '00000000000'", False),  # obvious placeholder
        ("amka = '12345678901'", False),  # documented fake
        ("amka = '15039912345'  # amka-ok", False),
        ("id 150399123456", False),  # 12 digits: not an AMKA
        ("sha " + "a" * 20 + "15039912345" + "b" * 20, False),  # amka-ok: inside a hash-like token
    ],
)
def test_amka_detection(repo, text, blocked):
    stage(repo, "notes.txt", text + "\n")
    assert bool(problems(repo)) is blocked


def test_private_denylist_blocks_terms_case_insensitively(repo):
    (repo / ".private-denylist").write_text("# real names\nΠαπαδοπούλου\n", encoding="utf-8")
    stage(repo, "notes.txt", "Therapist: ΠΑΠΑΔΟΠΟΥΛΟΥ\n")
    assert any("denylist" in p for p in problems(repo))
    stage(repo, "other.txt", "nothing private\n")
    assert not any("other.txt" in p for p in problems(repo))


def test_all_and_history_modes(repo):
    stage(repo, "ok.txt", "fine\n")
    git(repo, "commit", "-q", "-m", "init")
    assert guard.check_all(repo) == [] and guard.check_history(repo) == []
    stage(repo, "bad.docx", "x")
    git(repo, "commit", "-q", "-m", "oops")
    git(repo, "rm", "-q", "-f", "bad.docx")
    git(repo, "commit", "-q", "-m", "remove")
    assert guard.check_all(repo) == []  # gone from the tree...
    assert any("bad.docx" in p for p in guard.check_history(repo))  # ...but still in history


def test_cli_exit_codes(repo, capsys):
    stage(repo, "x.pdf", "x")
    assert guard.main(["--staged"], cwd=repo) == 1
    assert "FAILED" in capsys.readouterr().err
    git(repo, "rm", "-q", "-f", "--cached", "x.pdf")
    assert guard.main(["--staged"], cwd=repo) == 0


def test_history_mode_finds_denylisted_terms_even_after_removal(repo):
    (repo / ".private-denylist").write_text("Νικολάου\n", encoding="utf-8")
    stage(repo, "notes.txt", "contact: ΝΙΚΟΛΑΟΥ\n")
    git(repo, "commit", "-q", "-m", "leak")
    git(repo, "rm", "-q", "-f", "notes.txt")
    git(repo, "commit", "-q", "-m", "remove")
    assert guard.check_all(repo) == []
    assert any("denylist" in p for p in guard.check_history(repo))
