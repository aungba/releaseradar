"""Stage 2: mask secrets in patches before anything is sent to an LLM provider.

Variable *names* are kept (``STRIPE_KEY=«redacted»``) so env-var detection still works.
"""

import math
import re
from collections import Counter

from app.engine.globs import match_any
from app.integrations.git.base import FileDiff

MASK = "«redacted»"

_TOKEN_PATTERNS = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),  # AWS temporary key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),  # GitLab PAT
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),  # Stripe
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b"),  # OpenAI-style
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google API key
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),  # Groq
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),  # credentials in URLs (user:pass@host)
]

# Files where KEY=value lines are configuration, not code.
_CONFIG_FILES = (
    ".env", ".env.*", "*.env", "*.properties", "*.ini", "*.cfg", "*.conf", "*.toml",
    "*.yaml", "*.yml", "docker-compose*",
)  # fmt: skip
_KEYWORDS = "KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|CREDENTIALS?|DSN"
_SECRET_NAME = (
    rf"(?:(?:[A-Za-z0-9]+[_.\-])*(?i:{_KEYWORDS})(?:[_.\-][A-Za-z0-9]+)*"  # API_KEY, db.password
    r"|[a-z0-9]+(?:Key|Secret|Token|Password))"  # apiKey, clientSecret (case-sensitive)
)
_ASSIGNMENT = re.compile(
    rf"^(?P<lead>\s*(?:export\s+)?[\"']?(?P<name>{_SECRET_NAME})[\"']?\s*[=:]\s*)(?P<value>.+?)\s*$"
)
_PLACEHOLDER = re.compile(
    r"""^(?:["']{2}|["']?(?:\$\{?.*|<.*>|changeme|change-me|xxx+|\*+|todo|none|null|"""
    r"""placeholder|example|your[-_].*|replace[-_]?me)["']?)$""",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"""(["'])([A-Za-z0-9+/=_\-]{32,200})\1""")
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_ENTROPY_THRESHOLD = 4.5


def shannon_entropy(value: str) -> float:
    counts = Counter(value)
    return -sum(c / len(value) * math.log2(c / len(value)) for c in counts.values())


def _mask_high_entropy(text: str) -> tuple[str, int]:
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group(2)
        if shannon_entropy(value) < _ENTROPY_THRESHOLD:
            return match.group(0)  # hashes, hex SHAs, readable identifiers
        count += 1
        return f"{match.group(1)}{MASK}{match.group(1)}"

    return _QUOTED.sub(repl, text), count


def redact_patch(path: str, patch: str) -> tuple[str, int]:
    """Return (redacted patch, number of redactions)."""
    is_config = match_any(path, _CONFIG_FILES)
    out: list[str] = []
    total = 0
    in_private_key = False
    for line in patch.split("\n"):
        prefix, body = (line[:1], line[1:]) if line[:1] in "+- " and line else ("", line)
        if line.startswith("@@"):
            out.append(line)
            continue
        if in_private_key or _PRIVATE_KEY_BEGIN.search(body):
            in_private_key = not _PRIVATE_KEY_END.search(body)
            if not (_PRIVATE_KEY_BEGIN.search(body) or _PRIVATE_KEY_END.search(body)):
                body = MASK
                total += 1
            out.append(prefix + body)
            continue
        for pattern in _TOKEN_PATTERNS:
            body, n = pattern.subn(MASK, body)
            total += n
        if is_config:
            match = _ASSIGNMENT.match(body)
            value = match.group("value") if match else ""
            if match and MASK not in value and not _PLACEHOLDER.match(value):
                body = match.group("lead") + MASK
                total += 1
        body, n = _mask_high_entropy(body)
        total += n
        out.append(prefix + body)
    return "\n".join(out), total


def redact(file: FileDiff) -> tuple[FileDiff, int]:
    if not file.patch:
        return file, 0
    patch, count = redact_patch(file.path, file.patch)
    return (file.model_copy(update={"patch": patch}), count) if count else (file, 0)
