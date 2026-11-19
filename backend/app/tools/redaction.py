"""Mask secrets in collected output before it is stored or shown to a model.

Diagnostic output (process command lines, logs, service definitions) routinely
contains credentials. Patterns are deliberately broad: a masked false positive costs
little, a leaked password costs a lot. Raw output is never persisted.
"""

from __future__ import annotations

import re

MASK = "[REDACTED]"

_SIMPLE: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private keys (whole block, or to the end if truncated)
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        f"-----BEGIN PRIVATE KEY----- {MASK} -----END PRIVATE KEY-----",
    ),
    # Authorization headers, bearer/basic tokens
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?\S+"), rf"\1{MASK}"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {MASK}"),
    # user:password@ in URLs and connection strings
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+):([^\s@/]+)@"), rf"\1:{MASK}@"),
    # Well-known token shapes
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), MASK),
)

# A regex of secret *names* (not a secret itself).
_SECRET_KEY = (
    r"[A-Za-z0-9_.-]*?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"  # noqa: S105
    r"private[_-]?key|client[_-]?secret|credentials?|session[_-]?id)[A-Za-z0-9_.-]*"
)
# key=value, key: value, "key": "value"
_ASSIGNED = re.compile(
    rf"(?i)(?P<key>\"?{_SECRET_KEY}\"?)(?P<sep>\s*[=:]\s*)(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;&\"'}}]+)"
)
# --password value / -p value style command-line options
_OPTION = re.compile(rf"(?i)(?P<opt>(?<!\w)--?{_SECRET_KEY})(?P<sep>\s+)(?P<value>[^\s-][^\s]*)")

# Prose like "password: is required" or "token=expired" is not a secret.
_PROSE = {"", "is", "was", "required", "missing", "invalid", "expired", "none", "null", "unset"}


def _mask_value(match: re.Match[str]) -> str:
    value = match.group("value")
    bare = value.strip("\"'").lower()
    if bare in _PROSE or bare == MASK.lower():
        return match.group(0)
    quote = value[0] if value[:1] in "\"'" and len(value) > 1 else ""
    return f"{match.group(1)}{match.group('sep')}{quote}{MASK}{quote}"


def redact(text: str) -> str:
    """Return ``text`` with secrets masked."""
    if not text:
        return text
    out = text
    for pattern, replacement in _SIMPLE:
        out = pattern.sub(replacement, out)
    out = _ASSIGNED.sub(_mask_value, out)
    return _OPTION.sub(_mask_value, out)


def cap(text: str, limit: int) -> str:
    """Trim to ``limit`` bytes (UTF-8), keeping the head and the tail."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    half = max(limit // 2 - 40, 0)
    head = data[:half].decode("utf-8", "ignore")
    tail = data[-half:].decode("utf-8", "ignore") if half else ""
    omitted = len(data) - 2 * half
    return f"{head}\n… [{omitted} bytes omitted] …\n{tail}"
