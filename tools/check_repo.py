#!/usr/bin/env python3
"""Repository safety check: keep private data out of Git.

Blocks
  * document / database files (DOCX, PDF, SQLite ...), wherever they appear
  * files under private directories (templates/, storage/, spike/assets/ ...)
  * .env files (except .env.example) and the local denylist itself
  * unknown binary files (small image/font types are allowed under frontend/)
  * AMKA-like numbers: 11 consecutive digits (obvious test values are allowed)
  * any term listed in the optional, git-ignored ``.private-denylist`` file
    (one term per line; use it for real patient/therapist names, never commit it).
    Matching ignores case and accents, so "Παπαδοπούλου" also catches "ΠΑΠΑΔΟΠΟΥΛΟΥ".

Usage (standard library only, any Python 3.8+):
  python tools/check_repo.py --staged     # what is about to be committed (pre-commit hook)
  python tools/check_repo.py --all        # every tracked file (index version)
  python tools/check_repo.py --history    # every commit on every branch

A line containing ``amka-ok`` is exempt from the AMKA check (for fake numbers
in tests). Do not use it for real data.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

BLOCKED_SUFFIXES = {
    ".docx", ".docm", ".dotx", ".dotm", ".doc",
    ".pdf",
    ".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3",
}
PRIVATE_PREFIXES = (
    "templates/", "storage/", "data/", "backups/", "previews/", "output/", "generated/",
    "spike/templates/", "spike/assets/", "spike/out/", "spike/review/",
)
ALLOWED_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2"}
ALLOWED_BINARY_PREFIX = "frontend/"
DENYLIST_NAME = ".private-denylist"
NUL = b"\x00"

# 11 digits not touching another letter/digit, so hashes and longer numbers do not match
AMKA_RE = re.compile(r"(?<![0-9A-Za-z])[0-9]{11}(?![0-9A-Za-z])")
# the same pattern as a POSIX ERE for `git grep -E` (history mode)
AMKA_ERE = r"(^|[^0-9A-Za-z])[0-9]{11}([^0-9A-Za-z]|$)"
AMKA_FAKE_VALUES = {"12345678901", "01234567890", "10987654321"}
AMKA_OK_MARKER = "amka-ok"


def run_git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def git_text(repo: Path, *args: str) -> str:
    return run_git(repo, *args).decode("utf-8", errors="replace")


def repo_root(start: Path) -> Path:
    return Path(git_text(start, "rev-parse", "--show-toplevel").strip())


def fold(text: str) -> str:
    """Case- and accent-insensitive form. Greek capitals carry no accents, so
    'Παπαδοπούλου' and 'ΠΑΠΑΔΟΠΟΥΛΟΥ' must compare equal (final sigma is folded too)."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def is_fake_amka(value: str) -> bool:
    return len(set(value)) == 1 or value in AMKA_FAKE_VALUES  # 00000000000, 11111111111, ...


def path_violations(path: str) -> list[str]:
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    out = []
    if any(name.lower().endswith(s) for s in BLOCKED_SUFFIXES):
        out.append("document/database file")
    if any(p.startswith(prefix) for prefix in PRIVATE_PREFIXES):
        out.append("inside a private directory")
    if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
        out.append("environment file (use .env.example for documented defaults)")
    if name == DENYLIST_NAME:
        out.append("the local denylist must stay private")
    return out


def load_denylist(repo: Path) -> list[str]:
    f = repo / DENYLIST_NAME
    if not f.is_file():
        return []
    terms = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(fold(line))
    return terms


def content_violations(path: str, data: bytes, denylist: list[str]) -> list[str]:
    p = path.replace("\\", "/")
    if NUL in data:
        name = p.rsplit("/", 1)[-1]
        suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if suffix in ALLOWED_BINARY_SUFFIXES and p.startswith(ALLOWED_BINARY_PREFIX):
            return []
        return ["binary file (only small images/fonts under frontend/ are allowed)"]
    out = []
    text = data.decode("utf-8", errors="replace")
    if denylist:
        folded = fold(text)
        if any(term in folded for term in denylist):
            out.append("contains a term from your private denylist")
    for lineno, line in enumerate(text.splitlines(), 1):
        if AMKA_OK_MARKER in line:
            continue
        if any(not is_fake_amka(m.group()) for m in AMKA_RE.finditer(line)):
            out.append(f"line {lineno}: AMKA-like number (11 digits)")
            break
    return out


def check_paths(repo: Path, paths: list[str], reader) -> list[str]:
    denylist = load_denylist(repo)
    problems = []
    for path in paths:
        issues = path_violations(path)
        folded_path = fold(path)
        if any(term in folded_path for term in denylist):
            issues.append("path contains a term from your private denylist")
        if not any(i.startswith(("document", "environment", "the local")) for i in issues):
            issues += content_violations(path, reader(path), denylist)
        problems += [f"{path}: {i}" for i in issues]
    return problems


def split_z(raw: str) -> list[str]:
    return [p for p in raw.split("\0") if p]


def check_staged(repo: Path) -> list[str]:
    raw = git_text(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return check_paths(repo, split_z(raw), lambda p: run_git(repo, "show", f":{p}"))


def check_all(repo: Path) -> list[str]:
    raw = git_text(repo, "ls-files", "-z")
    return check_paths(repo, split_z(raw), lambda p: run_git(repo, "show", f":{p}"))


def check_history(repo: Path) -> list[str]:
    problems = []
    names = set(git_text(repo, "log", "--all", "--name-only", "--pretty=format:").splitlines())
    for n in sorted(x for x in names if x):
        problems += [f"history: {n}: {i}" for i in path_violations(n)]
    revs = git_text(repo, "rev-list", "--all").split()
    if not revs:
        return problems

    proc = subprocess.run(["git", "grep", "-I", "-n", "-E", AMKA_ERE, *revs], cwd=repo,
                          capture_output=True, check=False)
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        if AMKA_OK_MARKER not in line and any(
            not is_fake_amka(m.group()) for m in AMKA_RE.finditer(line)
        ):
            problems.append(f"history: AMKA-like number: {line[:120]}")

    terms = load_denylist(repo)
    if terms:  # `git grep` cannot fold accents, so read each distinct blob once
        seen: set[str] = set()
        for rev in revs:
            for entry in git_text(repo, "ls-tree", "-r", rev).splitlines():
                meta, name = entry.split("\t", 1)
                blob = meta.split()[2]
                if blob in seen:
                    continue
                seen.add(blob)
                data = run_git(repo, "cat-file", "-p", blob)
                if NUL not in data and any(t in fold(data.decode("utf-8", "replace")) for t in terms):
                    problems.append(f"history: {name}: contains a term from your private denylist")
    return problems


def main(argv: list[str] | None = None, cwd: Path | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="check staged changes (default)")
    mode.add_argument("--all", action="store_true", help="check every tracked file")
    mode.add_argument("--history", action="store_true", help="check all commits")
    args = ap.parse_args(argv)
    repo = repo_root(cwd or Path.cwd())
    problems = check_all(repo) if args.all else check_history(repo) if args.history else check_staged(repo)
    if problems:
        print("Repository safety check FAILED. Nothing was committed.\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nPrivate data must never enter Git. Unstage the file (git restore --staged <file>).\n"
              "A fake test number can be marked with 'amka-ok' on its line.", file=sys.stderr)
        return 1
    print("Repository safety check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
