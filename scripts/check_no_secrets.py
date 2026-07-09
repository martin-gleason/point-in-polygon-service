#!/usr/bin/env python3
"""H3 — fail if a tracked file looks like it contains a committed secret.

Enforces the SPEC.md §9 non-negotiable "provider credentials never hardcoded or
committed": credentials are referenced by the *name* of an environment variable
in config (e.g. `token_env = "COOK_GEOCODER_TOKEN"`), never by value. This scan
is deliberately narrow — it looks for high-signal secret shapes (private-key
blocks, AWS keys, assigned secret literals) so it almost never false-positives,
and it is run both locally and in CI (.github/workflows/ci.yml).

    python scripts/check_no_secrets.py    # exit 0 clean, 1 if a secret is found
    python scripts/check_no_secrets.py --selftest   # prove the matcher itself

Standard library only — runs anywhere with no install.

The matcher distinguishes a hardcoded secret *value* from a reference to one.
An assignment trips only when a secret-named identifier (with any prefix, so
`db_password` and `COOK_GEOCODER_TOKEN` count, not just bare `token`) is set to
a **quoted string literal**. The env-reference forms config actually uses —
`token_env = "NAME"`, `token = os.environ["NAME"]`, `getenv("NAME")` — do not
match, because the secret word is not immediately followed by `= "<literal>"`.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

# High-signal patterns. Each targets a literal secret *value*, not a reference.
PATTERNS = [
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "private key block"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    # A secret-named identifier (optionally prefixed: db_, stripe_, COOK_GEOCODER_)
    # assigned a quoted literal of 8+ chars. The leading (?:^|[^\w-]) + [\w-]*
    # lets a prefix through while still anchoring; `token_env = "..."` does NOT
    # match because after `token` comes `_env`, not the `[:=]` assignment.
    (
        re.compile(
            r"""(?ix)
            (?:^|[^\w-])                                        # start, or a non-name char
            [\w-]*                                              # optional identifier prefix
            (?:api[_-]?key|secret|token|password|passwd)        # a secret-ish name
            \s*[:=]\s*                                          # assignment
            ['"][^'"\n]{8,}['"]                                 # a quoted literal value, 8+ chars
            """
        ),
        "assigned secret literal",
    ),
]

# Paths whose job is to talk about secrets (this scanner) — skip them.
SKIP_PREFIXES = ("scripts/check_no_secrets.py",)


def scan_line(line):
    """Return the label of the first secret pattern the line matches, or None."""
    for pattern, label in PATTERNS:
        if pattern.search(line):
            return label
    return None


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
            label = scan_line(line)
            if label:
                findings.append((path, lineno, label, line.strip()))
    return findings


# (line, should_flag) — the matcher's contract, including the reviewer's bypass
# cases. Run with --selftest; also guarded by tests if desired.
SELFTEST_CASES = [
    # Must FLAG — hardcoded secret literals, prefixed or bare, comment or not.
    ('api_key = "sk-realhardcodedvalue123"  # normally set via environment', True),
    ('token = "ghp_anotherrealsecret99"  # TODO move to env', True),
    ('db_password = "hunter2secret"', True),
    ('stripe_secret = "sk_live_abc12345"', True),
    ('github_token = "ghp_realtokenvalue123"', True),
    ('COOK_GEOCODER_TOKEN = "literal-token-committed-here"', True),
    ('password = "hunter2secret"', True),
    ("AKIAIOSFODNN7EXAMPLE", True),
    ("-----BEGIN RSA PRIVATE KEY-----", True),
    # Must NOT flag — the env-reference forms config uses (D2), and near-misses.
    ('token_env = "COOK_GEOCODER_TOKEN"', False),
    ('token = os.environ["COOK_GEOCODER_TOKEN"]', False),
    ('api_key = os.getenv("SOME_KEY")', False),
    ('token_env: str = "PROVIDER_TOKEN_ENV"', False),
    ('secret_env_var = "MY_SECRET_NAME"', False),
    ("# describes the token_env config field", False),
]


def selftest():
    failures = []
    for line, should_flag in SELFTEST_CASES:
        flagged = scan_line(line) is not None
        if flagged != should_flag:
            failures.append((line, should_flag, flagged))
    if failures:
        print("SELFTEST FAILED:", file=sys.stderr)
        for line, expected, got in failures:
            verb = "should flag" if expected else "should NOT flag"
            print(f"  {verb}: {line!r} (got flagged={got})", file=sys.stderr)
        return 1
    print(f"selftest passed ({len(SELFTEST_CASES)} cases).")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--selftest", action="store_true", help="verify the matcher against known cases"
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

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
