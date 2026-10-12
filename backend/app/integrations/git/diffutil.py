"""Diff helpers shared by all providers."""

import difflib

_BINARY_SNIFF_BYTES = 8192


def strip_file_headers(patch: str) -> str:
    """Drop anything before the first ``@@`` hunk (``diff --git``/``---``/``+++`` headers)."""
    if patch.startswith("@@"):
        return patch
    index = patch.find("\n@@")
    return patch[index + 1 :] if index != -1 else patch


def count_changes(patch: str | None) -> tuple[int, int]:
    """Count added/removed lines inside unified hunks.

    Lines before the first ``@@`` are ignored, so file headers never count, while a removed
    line that itself starts with ``--`` (rendered ``--- ...``) inside a hunk still does.
    """
    if not patch:
        return 0, 0
    additions = deletions = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif not in_hunk:
            continue
        elif line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def build_patch(old_text: str, new_text: str, context: int = 3) -> str:
    """Unified hunks between two texts, in the same headerless format providers return."""
    lines = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        n=context,
    )
    body = list(lines)[2:]  # drop '--- ' / '+++ ' header lines
    return "".join(line if line.endswith("\n") else line + "\n" for line in body)


def looks_binary(content: bytes) -> bool:
    return b"\0" in content[:_BINARY_SNIFF_BYTES]


def decode_text(content: bytes) -> str | None:
    """UTF-8 text, or None when the content is binary/undecodable."""
    if looks_binary(content):
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None
