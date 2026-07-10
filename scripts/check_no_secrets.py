"""Block staged changes that would leak a wallet address or private key.

Run as a git pre-commit hook. Install with:

    python scripts/check_no_secrets.py --install

This repo is public and shared. Wallet addresses are permanent on-chain
identifiers: committing one links this repo to that person's full Polymarket
betting history. Private keys are worse. Neither belongs in git.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 0x + exactly 40 hex chars. The prefix is case-insensitive (0x or 0X); the
# trailing guard prevents matching the leading 40 chars of a 64-hex
# conditionId, which is legitimate market data.
WALLET_RE = re.compile(r"0[xX][a-fA-F0-9]{40}(?![a-fA-F0-9])")

# 0x + exactly 64 hex chars is ambiguous: it is both a conditionId (safe) and a
# raw private key (catastrophic). We only flag it near key-ish words.
PRIVKEY_RE = re.compile(
    r"(?i)(private[_-]?key|privkey|secret[_-]?key|seed[_-]?phrase|mnemonic)"
)

# Placeholders that are safe to commit.
ALLOWED = {
    "0x" + "0" * 40,
    "0x" + "1" * 40,
    "0x" + "d" * 40,
    "0xdeadbeef" + "0" * 32,
}

# Files exempt from content scanning. These describe or exercise the detection
# patterns themselves, so they legitimately contain address-shaped fixtures.
# `.env.example` is deliberately NOT here: it is content-scanned so a real
# wallet pasted "as an example" is caught (its placeholder is in ALLOWED).
ALLOWED_PATHS = {
    "scripts/check_no_secrets.py",
    ".gitignore",
    # The scanner's own adversarial test suite must contain non-placeholder
    # addresses to prove the scanner catches them.
    "tests/adversarial/test_secret_scanner.py",
}

# Files that must never be committed at all, whatever their contents. Matched
# case-insensitively: on a case-insensitive filesystem (Windows/macOS) the app
# writes data/foo.db but `git add Data/foo.db` would otherwise slip past.
FORBIDDEN_PATHS = re.compile(
    r"(^|/)\.env$"                       # the real .env
    r"|(^|/)\.env\.(?!example$)[^/]*$"   # .env.local etc, but keep .env.example
    r"|(^|/)data/.*\.(db|sqlite3?)$"     # any local database
    r"|\.wallet$",
    re.IGNORECASE,
)


def staged_files() -> list[str]:
    # Include R (renames): a `git mv` + light edit is classified as a rename,
    # and the default ACM filter would skip it entirely, letting a secret in a
    # renamed file slip through.
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        check=True,
    )
    text = out.stdout.decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line.strip()]


def staged_blob(path: str) -> str:
    out = subprocess.run(
        ["git", "show", f":{path}"], capture_output=True, check=False
    )
    if out.returncode != 0:
        return ""
    # Decode bytes as UTF-8 with replacement rather than relying on the
    # platform default (cp1252 on Windows). A binary blob -- e.g. a force-added
    # .db -- used to crash the decode; the exception was swallowed and the file
    # treated as clean. Replacement keeps the hook alive AND still surfaces any
    # ASCII wallet bytes embedded in that binary.
    return out.stdout.decode("utf-8", errors="replace")


def main() -> int:
    if "--install" in sys.argv:
        return install()

    problems: list[str] = []

    for path in staged_files():
        if FORBIDDEN_PATHS.search(path):
            problems.append(f"{path}: this file must never be committed")
            continue

        if path in ALLOWED_PATHS:
            continue

        content = staged_blob(path)
        if not content:
            continue

        for lineno, line in enumerate(content.splitlines(), start=1):
            for match in WALLET_RE.finditer(line):
                if match.group(0).lower() in ALLOWED:
                    continue
                problems.append(
                    f"{path}:{lineno}: wallet address {match.group(0)[:10]}..."
                )
            if PRIVKEY_RE.search(line) and re.search(r"0x[a-fA-F0-9]{64}", line):
                problems.append(f"{path}:{lineno}: looks like a private key")

    if problems:
        sys.stderr.write("\nBLOCKED: refusing to commit secrets.\n\n")
        for problem in problems:
            sys.stderr.write(f"  {problem}\n")
        sys.stderr.write(
            "\nWallet addresses belong in .env or data/*.db, both gitignored.\n"
            "Nothing in this repo should ever need a private key.\n"
            "Override only if you are certain: git commit --no-verify\n\n"
        )
        return 1

    return 0


HOOK_SHIM = '''#!/bin/sh
# Runs the secret scanner using this repo's venv interpreter only.
# Never falls back to a system python: a missing venv must fail loudly, not
# silently skip the check that keeps wallet addresses out of this repo.
root="$(git rev-parse --show-toplevel)"
for py in "$root/.venv/Scripts/python.exe" "$root/.venv/bin/python"; do
    if [ -x "$py" ]; then
        exec "$py" "$root/scripts/check_no_secrets.py"
    fi
done
echo "pre-commit: no .venv found. Create it, then retry:" >&2
echo "  py -3.13 -m venv .venv && .venv/Scripts/pip install -r requirements.txt" >&2
exit 1
'''


def install() -> int:
    hooks = REPO_ROOT / ".git" / "hooks"
    if not hooks.is_dir():
        sys.stderr.write("No .git/hooks directory. Are you in the repo root?\n")
        return 1

    hook = hooks / "pre-commit"
    hook.write_text(HOOK_SHIM, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    sys.stdout.write(f"Installed pre-commit hook at {hook}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
