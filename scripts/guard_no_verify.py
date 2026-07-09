"""PreToolUse guard: refuse Bash commands that bypass the secret-scanning hook.

`git commit --no-verify` (and its `-n` short form) skips .git/hooks/pre-commit,
which is the only thing standing between a wallet address and a public repo.

Reads the hook payload on stdin, writes a permission decision on stdout.
Exits 0 always: a crash here must not wedge the session, and a non-blocking
failure is visible in the transcript.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

REASON = (
    "Blocked: --no-verify skips the pre-commit secret scan, which is what keeps "
    "wallet addresses and private keys out of this public repo. If the hook is "
    "producing a false positive, fix the pattern in scripts/check_no_secrets.py "
    "rather than bypassing the scan."
)


def bypasses_hooks(command: str) -> bool:
    # Split on shell separators so `foo && git commit -n` is still caught.
    for segment in re.split(r"&&|\|\||;|\|", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if len(tokens) < 2 or tokens[0] != "git":
            continue
        if tokens[1] not in ("commit", "push"):
            continue
        for token in tokens[2:]:
            if token == "--no-verify":
                return True
            # `-n` means --no-verify for commit, but --dry-run for push. Only
            # commit is a bypass. Catch bundled short flags like `-am -n`, but
            # not `-m` values, which shlex has already split off as their own
            # token following -m.
            if tokens[1] == "commit" and re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", token):
                return True
    return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    command = (payload.get("tool_input") or {}).get("command") or ""
    if not bypasses_hooks(command):
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": REASON,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
