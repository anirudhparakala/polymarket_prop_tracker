"""Adversarial tests for scripts/check_no_secrets.py and scripts/guard_no_verify.py.

This suite tries to defeat the two defenses that are supposed to keep a
wallet address (or a private key) out of this public repo: the pre-commit
secret scanner, and the PreToolUse guard that blocks `git commit
--no-verify`. It also probes .gitignore for gaps that would let a database
or env file get staged in the first place.

Every test is offline and deterministic. Tests that exercise real git
plumbing (rename detection, `git show` decoding) do so inside a disposable
`tmp_path` git repository -- never the real repo -- and never leave
anything staged anywhere. Only throwaway, made-up addresses are used
(`0x` + repeated hex digits); none of them are real.

Findings are written up in .superpowers/adversarial/security-findings.md.
Real leak paths (false negatives that let a wallet through, or gitignore
gaps) are encoded here as `@pytest.mark.xfail(strict=True, ...)`: the
assertion states the CORRECT behavior, which currently fails against the
actual (leaky) behavior. If a leak is ever closed, its test starts passing
and pytest reports XPASS -- a hard failure under strict=True -- which is
the point: the fix is caught automatically.

Run:
    .venv/Scripts/python.exe -m pytest tests/adversarial/test_secret_scanner.py -v --basetemp=.pytest_tmp/sec
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = REPO_ROOT / "scripts" / "check_no_secrets.py"
GUARD_PATH = REPO_ROOT / "scripts" / "guard_no_verify.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scanner = _load(SCANNER_PATH, "check_no_secrets_under_test")
guard = _load(GUARD_PATH, "guard_no_verify_under_test")

# Throwaway, made-up addresses. Not real, not the user's.
FAKE_WALLET = "0x" + "a" * 40
FAKE_WALLET_2 = "0x" + "b" * 40


def run_scanner_on(files: dict[str, str], monkeypatch) -> tuple[int, str]:
    """Simulate what the pre-commit hook sees for a given staged fileset,
    without touching git or leaving anything staged anywhere.
    """
    monkeypatch.setattr(scanner, "staged_files", lambda: list(files.keys()))
    monkeypatch.setattr(scanner, "staged_blob", lambda path: files.get(path, ""))
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        exit_code = scanner.main()
    return exit_code, buf.getvalue()


def _init_temp_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "adversarial@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "adversarial"], cwd=repo, check=True)
    return repo


# ---------------------------------------------------------------------------
# Defenses that hold today (green). These pin down the CORRECT current
# behavior so a future change can't silently regress it.
# ---------------------------------------------------------------------------


def test_wallet_address_detected_regardless_of_hex_digit_case(monkeypatch):
    # The "0x" prefix stays lowercase here -- WALLET_RE only case-folds the
    # 40 hex digits, not the prefix itself (see
    # test_uppercase_0x_prefix_not_detected below for that gap).
    all_upper = "0x" + FAKE_WALLET[2:].upper()
    mixed = "0x" + "".join(
        c.upper() if i % 2 == 0 else c for i, c in enumerate(FAKE_WALLET[2:])
    )
    for addr in (FAKE_WALLET, all_upper, mixed):
        exit_code, stderr = run_scanner_on({"leak.txt": f"my wallet is {addr}\n"}, monkeypatch)
        assert exit_code != 0, f"{addr} should have been flagged"
        assert "wallet address" in stderr


def test_conditionid_64hex_alone_not_falsely_flagged(monkeypatch):
    # A legitimate 64-hex conditionId must not be mistaken for a wallet or
    # a private key just because it starts with 40 hex-looking chars.
    cond_id = "0x" + "3" * 64
    exit_code, _ = run_scanner_on(
        {"positions.py": f'CONDITION_ID = "{cond_id}"'}, monkeypatch
    )
    assert exit_code == 0


def test_allowed_placeholder_addresses_pass(monkeypatch):
    content = "\n".join(
        f'X = "{a}"'
        for a in ("0x" + "0" * 40, "0x" + "1" * 40, "0x" + "d" * 40, "0xdeadbeef" + "0" * 32)
    )
    exit_code, _ = run_scanner_on({"placeholders.py": content}, monkeypatch)
    assert exit_code == 0


def test_private_key_with_keyword_detected(monkeypatch):
    line = "PRIVATE_KEY=0x" + "b" * 64
    exit_code, stderr = run_scanner_on({"notes.txt": line}, monkeypatch)
    assert exit_code != 0
    assert "private key" in stderr


def test_dotenv_path_hard_blocked_even_with_benign_content(monkeypatch):
    exit_code, stderr = run_scanner_on({".env": "# just a comment, no secrets\n"}, monkeypatch)
    assert exit_code != 0
    assert "must never be committed" in stderr


def test_forbidden_paths_blocks_data_dir_at_any_depth():
    # Mitigates (but does not fully close) the gitignore gap proven in
    # test_gitignore_data_subdir_db_not_ignored below: even though
    # `data/*.db` in .gitignore is not recursive, the scanner's own
    # FORBIDDEN_PATHS regex is, and hard-blocks a force-added db anywhere
    # under data/.
    assert scanner.FORBIDDEN_PATHS.search("data/positions.db")
    assert scanner.FORBIDDEN_PATHS.search("data/backups/2026-07-09/positions.db")


def test_gitignore_wal_shm_journal_sidecars_covered():
    for suffix in ("wal", "shm", "journal"):
        path = f"data/positions.db-{suffix}"
        result = subprocess.run(
            ["git", "check-ignore", "-v", path], cwd=REPO_ROOT, capture_output=True, text=True
        )
        assert result.returncode == 0, f"{path} should be gitignored but isn't: {result.stdout!r}"


def test_guard_blocks_no_verify_and_dash_n():
    assert guard.bypasses_hooks('git commit --no-verify -m "x"') is True
    assert guard.bypasses_hooks('git commit -n -m "x"') is True
    assert guard.bypasses_hooks('echo hi && git commit -n -m "x"') is True


def test_guard_allows_ordinary_commit():
    assert guard.bypasses_hooks('git commit -m "normal commit"') is False
    assert guard.bypasses_hooks("git push origin main") is False


def test_non_whitelisted_example_address_is_blocked_false_positive(monkeypatch):
    """Not a leak -- the opposite problem (Important, see findings report).

    ALLOWED only contains 4 specific placeholder addresses. Any other
    illustrative address (a tutorial example, a sequential placeholder used
    in a doc comment) is blocked identically to a genuine leak, with no way
    for the user to tell the difference from the error message alone. A
    scanner that cries wolf on legitimate content trains users to reach for
    --no-verify. This test documents the current (real) blocking behavior;
    it is intentionally NOT xfail because it is not a false negative.
    """
    doc_example = "0x1234567890123456789012345678901234567890"
    exit_code, stderr = run_scanner_on({"README.md": f"e.g. `{doc_example}`"}, monkeypatch)
    assert exit_code != 0
    assert "wallet address" in stderr


# ---------------------------------------------------------------------------
# LEAK: WALLET_RE / PRIVKEY_RE false negatives on content the scanner does
# examine.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="LEAK: splitting a 40-hex address across adjacent string literals "
    "(e.g. `\"0x\" + \"aaaa...\" + \"aaaa...\"`) leaves no 40 contiguous hex "
    "chars on any line, so WALLET_RE never fires.",
)
def test_concatenated_wallet_not_detected(monkeypatch):
    half1, half2 = FAKE_WALLET[2:22], FAKE_WALLET[22:]
    line = f'WALLET = "0x" + "{half1}" + "{half2}"'
    exit_code, _ = run_scanner_on({"leak.py": line}, monkeypatch)
    assert exit_code != 0


@pytest.mark.xfail(
    strict=True,
    reason="LEAK: a wallet left-padded to 32 bytes (0x + 24 zero hex + "
    "40-hex address = 64 hex total) -- exactly how addresses appear in raw "
    "eth_getLogs topics / Transfer event dumps -- is invisible: the "
    "negative lookahead added to avoid flagging 64-hex conditionIds also "
    "blinds WALLET_RE to any wallet immediately followed by more hex.",
)
def test_left_padded_wallet_not_detected(monkeypatch):
    padded = "0x" + "0" * 24 + FAKE_WALLET[2:]
    exit_code, _ = run_scanner_on({"log_dump.txt": f"topics: [{padded}]"}, monkeypatch)
    assert exit_code != 0


@pytest.mark.xfail(
    strict=True,
    reason="LEAK: a wallet address stored without its 0x prefix (as some "
    "raw hex/CSV dumps do) is not recognized at all, since WALLET_RE "
    "hard-requires a literal '0x' prefix.",
)
def test_prefixless_wallet_not_detected(monkeypatch):
    exit_code, _ = run_scanner_on({"raw.csv": FAKE_WALLET[2:]}, monkeypatch)
    assert exit_code != 0


# FIXED: WALLET_RE now matches a case-insensitive prefix (0x or 0X).
def test_uppercase_0x_prefix_is_detected(monkeypatch):
    addr = "0X" + FAKE_WALLET[2:]
    exit_code, _ = run_scanner_on({"leak.txt": f"wallet={addr}\n"}, monkeypatch)
    assert exit_code != 0


@pytest.mark.xfail(
    strict=True,
    reason="LEAK: PRIVKEY_RE only fires when a private-key-ish keyword "
    "(private_key/privkey/secret_key/seed_phrase/mnemonic) sits on the SAME "
    "line as a 64-hex value. A bare variable name like PK= or KEY= carries "
    "a raw private key straight past the scanner.",
)
def test_bare_named_private_key_not_detected(monkeypatch):
    line = "PK=0x" + "b" * 64
    exit_code, _ = run_scanner_on({"notes.txt": line}, monkeypatch)
    assert exit_code != 0


@pytest.mark.xfail(
    strict=True,
    reason="LEAK: the scanner only regexes file CONTENT plus a small "
    "path-pattern blocklist; it never checks whether the staged PATH itself "
    "contains a wallet address, so a file literally named after the wallet "
    "leaks it via the tracked filename even when its content is innocuous.",
)
def test_wallet_embedded_in_filename_not_detected(monkeypatch):
    path = f"exports/{FAKE_WALLET}_summary.txt"
    exit_code, _ = run_scanner_on({path: "nothing sensitive in here\n"}, monkeypatch)
    assert exit_code != 0


# ---------------------------------------------------------------------------
# LEAK: path-level gaps -- .gitignore and/or FORBIDDEN_PATHS miss real
# locations a database or backup can end up at.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="LEAK: db.init_db() accepts any caller-chosen path; a database "
    "placed outside the literal data/ directory (a custom DB_PATH, a manual "
    "backup) matches neither .gitignore's data/*.db rules nor the "
    "scanner's FORBIDDEN_PATHS hard-block regex, so it is treated as an "
    "ordinary trackable text file.",
)
def test_custom_path_database_not_recognized_as_special():
    path = "backup/positions.db"
    ignored = (
        subprocess.run(
            ["git", "check-ignore", "-v", path], cwd=REPO_ROOT, capture_output=True, text=True
        ).returncode
        == 0
    )
    forbidden = bool(scanner.FORBIDDEN_PATHS.search(path))
    assert ignored or forbidden


@pytest.mark.xfail(
    strict=True,
    reason="LEAK: a manually renamed backup like data/positions.db.bak "
    "matches neither .gitignore's data/*.db rule (extension is .bak, not "
    ".db) nor the scanner's FORBIDDEN_PATHS regex (which requires the path "
    "to end in .db/.sqlite/.sqlite3).",
)
def test_db_backup_extension_not_recognized():
    path = "data/positions.db.bak"
    ignored = (
        subprocess.run(
            ["git", "check-ignore", "-v", path], cwd=REPO_ROOT, capture_output=True, text=True
        ).returncode
        == 0
    )
    forbidden = bool(scanner.FORBIDDEN_PATHS.search(path))
    assert ignored or forbidden


@pytest.mark.xfail(
    strict=True,
    reason="LEAK (gitignore gap): data/*.db does not match subdirectories "
    "of data/ since '*' does not cross '/'. A dated backups folder under "
    "data/ is not recognized as ignored, so `git add data/` or `git add .` "
    "stages it with no visual 'ignored' cue in the user's editor/git "
    "status -- protection then depends entirely on the hook being "
    "installed and not bypassed.",
)
def test_gitignore_data_subdir_db_not_ignored():
    result = subprocess.run(
        ["git", "check-ignore", "-v", "data/nested/sub/positions.db"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# LEAK: the --no-verify guard and hook-installation gaps.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="LEAK: guard_no_verify.py only pattern-matches the literal "
    "--no-verify/-n tokens on a `git commit`/`git push` invocation. "
    "`git -c core.hooksPath=<empty-dir> commit ...` disables ALL hooks "
    "(including the secret scanner) without using either flag, and is not "
    "recognized as a bypass.",
)
def test_guard_misses_hookspath_override():
    cmd = 'git -c core.hooksPath=/dev/null commit -m "x"'
    assert guard.bypasses_hooks(cmd) is True


@pytest.mark.xfail(
    strict=True,
    reason="LEAK (structural): the pre-commit hook is never installed "
    "automatically. Git does not version .git/hooks, so every fresh clone "
    "of this public repo starts with zero protection until the user "
    "manually runs `python scripts/check_no_secrets.py --install`. Nothing "
    "in conftest.py, pytest.ini, or requirements.txt does this "
    "automatically or warns the user it's missing.",
)
def test_hook_installation_is_not_automated():
    haystack = "\n".join(
        (REPO_ROOT / name).read_text(encoding="utf-8")
        for name in ("conftest.py", "pytest.ini", "requirements.txt")
    )
    assert "check_no_secrets" in haystack or "pre-commit" in haystack.lower()


# ---------------------------------------------------------------------------
# LEAK: git-plumbing-level bypasses. These use a disposable tmp_path git
# repo -- never the real repo -- and never leave anything staged anywhere
# once the test process exits.
# ---------------------------------------------------------------------------


# FIXED: staged_files() now uses --diff-filter=ACMR, so a renamed-and-edited
# file is scanned instead of skipped.
def test_rename_and_edit_wallet_is_caught(tmp_path):
    repo = _init_temp_git_repo(tmp_path)
    filler = "line filler " * 200
    (repo / "a.txt").write_text(filler, encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    (repo / "b.txt").write_text(filler + f"\nWALLET={FAKE_WALLET}\n", encoding="utf-8")
    subprocess.run(["git", "rm", "-q", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)

    # Sanity check: confirm git really did classify this as a rename, not
    # a plain delete+add, before blaming the scanner for missing it.
    status = subprocess.run(
        ["git", "diff", "--cached", "--name-status"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status.startswith("R"), f"expected a detected rename, got: {status!r}"

    result = subprocess.run(
        [sys.executable, str(SCANNER_PATH)], cwd=repo, capture_output=True, text=True
    )
    assert result.returncode != 0, (
        "scanner should have blocked a commit staging a fresh wallet "
        f"address, but it exited 0. stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


# FIXED: staged_blob() reads bytes and decodes UTF-8 with errors="replace", so
# a lone undefined byte no longer crashes the reader thread; the surrounding
# ASCII wallet bytes still surface for the content scan.
def test_binary_content_with_undefined_byte_still_surfaces_wallet(tmp_path, monkeypatch):
    repo = _init_temp_git_repo(tmp_path)
    # 0x81 is undefined in cp1252 and also an invalid lone byte in UTF-8,
    # so this reproduces the crash on either default codec.
    blob = b"binary header junk " + FAKE_WALLET_2.encode() + b" trailer \x81 more junk\n"
    (repo / "dump.bin").write_bytes(blob)
    subprocess.run(["git", "add", "dump.bin"], cwd=repo, check=True)

    monkeypatch.chdir(repo)
    content = scanner.staged_blob("dump.bin")

    assert FAKE_WALLET_2 in content, (
        "staged_blob() should return the file's real text content (and "
        f"thus the wallet address within it) but returned {content!r} "
        "-- the decoder crashed in a background thread and the exception "
        "was swallowed, so the caller sees empty content instead of an "
        "error."
    )
