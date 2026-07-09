#!/usr/bin/env python3
"""H3 — fail if a tracked file looks like it contains a committed secret.

Enforces the SPEC.md §9 non-negotiable "provider credentials never hardcoded or
committed": credentials are referenced by the *name* of an environment variable
in config (e.g. `token_env = "COOK_GEOCODER_TOKEN"`), never by value. This scan
is deliberately narrow — it looks for high-signal secret shapes (private-key
blocks, AWS keys, assigned secret literals) so it almost never false-positives,
and it is run both locally and in CI (.github/workflows/ci.yml).

    python scripts/check_no_secrets.py    # exit 0 clean, 1 if a secret is found

Standard library only — runs anywhere with no install.
"""
from __future__ import annotations

import re
import subprocess
import sys

# High-signal patterns. Each is a literal secret *value*, not a reference to one.
PATTERNS = [
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "private key block"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    # An assignment of a non-trivial literal to a secret-named key. The env-var
    # *reference* form (token_env = "NAME", token = os.environ[...]) is allowed;
    # only a quoted literal value assigned to token/secret/api_key/password trips.
    (
        re.compile(
            r"""(?ix)
            \b(?:api[_-]?key|secret|token|password|passwd)\b   # a secret-ish name
            \s*[:=]\s*                                          # assignment
            ['"][^'"\n]{8,}['"]                                 # a quoted literal, 8+ chars
            """
        ),
        "assigned secret literal",
    ),
]

# Reference forms that must NOT trip the scanner even though they mention a
# secret-ish word next to an assignment.
ALLOWED = re.compile(
    r"""(?ix)
    (?:token_env|_env|env|environ)          # env-var reference conventions
    | os\.environ
    | getenv
    """
)

# Paths whose job is to talk about secrets (this scanner, docs) — skip them.
SKIP_PREFIXES = ("scripts/check_no_secrets.py",)


def tracked_text_files():
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for path in out:
        if path.startswith(SKIP_PREFIXES):
            continue
        yield path


def scan():
    findings = []
    for path in tracked_text_files():
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except (UnicodeDecodeError, FileNotFoundError):
            continue  # binary (e.g. the GeoPackage) or removed — nothing to scan
        for lineno, line in enumerate(lines, 1):
            for pattern, label in PATTERNS:
                if pattern.search(line) and not ALLOWED.search(line):
                    findings.append((path, lineno, label, line.strip()))
    return findings


def main():
    findings = scan()
    if not findings:
        print("no committed secrets found.")
        return 0
    print("POSSIBLE COMMITTED SECRETS — SPEC §9 violation:", file=sys.stderr)
    for path, lineno, label, text in findings:
        print(f"  {path}:{lineno}: {label}: {text}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
