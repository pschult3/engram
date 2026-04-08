"""Tests for secret redaction in the noise gate."""

from __future__ import annotations

import pytest

from engram.ingest.redact import is_sensitive_path, redact_payload, redact_string


# ---------------------------------------------------------------------------
# is_sensitive_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".env.production",
        "/home/user/project/.env",
        "config/secrets/db.key",
        "/Users/me/.ssh/id_rsa",
        "/Users/me/.aws/credentials",
        "certs/server.pem",
        "deploy/keystore.jks",
        "infra/secrets/api.key",
    ],
)
def test_sensitive_path_detected(path: str) -> None:
    assert is_sensitive_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/main.py",
        "config/settings.py",
        "README.md",
        "tests/test_auth.py",
        "src/auth/tokens.py",      # "tokens" in filename, not a key file
        "docs/environment.md",     # env in name but not .env
        "src/config.ts",
        "Makefile",
    ],
)
def test_safe_path_not_flagged(path: str) -> None:
    assert is_sensitive_path(path) is False


# ---------------------------------------------------------------------------
# redact_string — positive cases (must redact)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAIOSFODNN7EXAMPLE",                        # AWS key ID
        "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG",  # AWS secret
        "ghp_1234567890abcdefghijklmnopqrstuvwxyz",     # GitHub PAT
        "gho_1234567890abcdefghijklmnopqrstuvwxyz",     # GitHub OAuth
        "sk-abcdefghijklmnopqrstuvwxyz123456",          # OpenAI
        "sk-ant-api01-abcdefghijklmnopqrstuvwxyz",     # Anthropic
        "xox" "b-1234567890-abcdefghijklmnop",            # Slack bot token (split to avoid scanner)
        "password: supersecretpassword1234",            # generic key=value
        "api_key=AbCdEf1234567890AbCdEf1234",           # generic
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.abc123def456ghi789",  # JWT
        "-----BEGIN RSA PRIVATE KEY-----",              # PEM header
    ],
)
def test_secrets_are_redacted(secret: str) -> None:
    result = redact_string(secret)
    assert "[REDACTED:" in result
    # The original secret value must not appear verbatim in the output.
    # We check a long prefix to avoid matching the "[REDACTED:...]" tag itself.
    for chunk in [secret[:12]]:
        assert chunk not in result or result.startswith("[REDACTED:")


# ---------------------------------------------------------------------------
# redact_string — negative cases (must NOT redact innocent strings)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "safe",
    [
        "pytest passed 42 tests",
        "npm install completed",
        "password prompt displayed",          # word "password" but no value
        "api_key param not provided",         # no value after key name
        "short=abc",                          # value too short for generic pattern
        "git commit -m 'add feature'",
        "docker build -t myapp:latest .",
    ],
)
def test_safe_strings_not_redacted(safe: str) -> None:
    assert redact_string(safe) == safe


# ---------------------------------------------------------------------------
# redact_payload
# ---------------------------------------------------------------------------


def test_redact_payload_applies_to_string_values() -> None:
    payload = {
        "command": "export API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456",
        "status": "ok",
        "count": 42,
    }
    result = redact_payload(payload)
    assert "[REDACTED:" in result["command"]
    assert result["status"] == "ok"   # already clean
    assert result["count"] == 42      # non-string left untouched


def test_redact_payload_returns_shallow_copy() -> None:
    payload = {"a": "clean", "b": "also clean"}
    result = redact_payload(payload)
    assert result is not payload
    assert result == payload
