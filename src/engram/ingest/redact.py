"""Redact secret patterns from event payloads before they reach the queue.

Patterns are intentionally hardcoded — no user configuration.
Two mechanisms:
  1. Path denylist: drop the entire event if the file path looks sensitive.
  2. String redaction: replace known secret patterns with [REDACTED:<kind>].
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re

log = logging.getLogger("engram.redact")

# ---------------------------------------------------------------------------
# Path denylist — events touching these paths are dropped entirely.
# ---------------------------------------------------------------------------

_SENSITIVE_PATH_PATTERNS = (
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/id_rsa",
    "**/id_rsa.*",
    "**/id_dsa",
    "**/id_ecdsa",
    "**/id_ed25519",
    "**/id_ed25519.*",
    "**/.ssh/**",
    "**/.aws/**",
    "**/credentials",
    "**/secrets/**",
    "**/.netrc",
    "**/keystore*",
    "**/*.jks",
    "**/*.keystore",
)

# ---------------------------------------------------------------------------
# Regex patterns — applied to string values in event payloads.
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # AWS access key ID
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws-key"),
    # AWS secret access key (key=value form)
    (re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S+"), "aws-secret"),
    # GitHub tokens (fine-grained and classic)
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "github-token"),
    # GitHub classic personal access token
    (re.compile(r"ghp_[A-Za-z0-9]{36,}"), "github-token"),
    # OpenAI key
    (re.compile(r"sk-[A-Za-z0-9\-_]{20,}"), "openai-key"),
    # Anthropic key (more specific, checked before generic sk-)
    (re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"), "anthropic-key"),
    # Slack tokens
    (re.compile(r"xox[bpoa]-[A-Za-z0-9\-]{10,}"), "slack-token"),
    # Generic key=value patterns — require at least 16 chars in the value
    # to reduce false positives (e.g. short env vars like FOO=bar).
    (
        re.compile(
            r"(?i)(?:api[_\-]?key|access[_\-]?token|auth[_\-]?token|"
            r"password|passwd|secret|private[_\-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+=]{16,}"
        ),
        "generic-secret",
    ),
    # JWT-shaped tokens (three base64url segments separated by dots)
    (
        re.compile(
            r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
        ),
        "jwt",
    ),
    # PEM private key header — drop everything from here
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private-key-block"),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_sensitive_path(path: str) -> bool:
    """Return True if *path* matches any sensitive-file pattern.

    When True the caller should drop the entire event rather than trying
    to redact individual fields.
    """
    path = path.replace("\\", "/")
    basename = os.path.basename(path)
    for pattern in _SENSITIVE_PATH_PATTERNS:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern):
            log.debug("Dropping event for sensitive path: %s", path)
            return True
        # For bare filenames (no parent dir), also match against the last
        # component of the pattern so "**/.env" matches ".env" directly.
        if not pattern.endswith("/**"):
            basename_pat = pattern.rsplit("/", 1)[-1]
            if fnmatch.fnmatch(basename, basename_pat):
                log.debug("Dropping event for sensitive path: %s", path)
                return True
    return False


def redact_string(s: str) -> str:
    """Replace known secret patterns in *s* with [REDACTED:<kind>].

    Operates on the full string; multiple patterns may match.
    """
    if not s:
        return s
    for pattern, kind in _SECRET_PATTERNS:
        s = pattern.sub(f"[REDACTED:{kind}]", s)
    return s


def redact_payload(payload: dict) -> dict:
    """Return a shallow copy of *payload* with all string values redacted."""
    return {k: redact_string(v) if isinstance(v, str) else v for k, v in payload.items()}
