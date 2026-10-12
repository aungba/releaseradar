"""Diff helper unit tests."""

import pytest

from app.integrations.git.diffutil import (
    build_patch,
    count_changes,
    decode_text,
    looks_binary,
    strip_file_headers,
)


def test_count_changes_counts_hunk_lines_only() -> None:
    patch = "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n ctx\n-old\n+new\n+++ added plus line\n"
    assert count_changes(patch) == (2, 1)


def test_count_changes_counts_removed_sql_comment() -> None:
    # Removing the line "-- note" renders as "--- note" inside the hunk.
    assert count_changes("@@ -1 +0,0 @@\n--- note\n") == (0, 1)


def test_count_changes_ignores_no_newline_marker() -> None:
    assert count_changes("@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n") == (1, 1)


@pytest.mark.parametrize("patch", [None, ""])
def test_count_changes_empty(patch: str | None) -> None:
    assert count_changes(patch) == (0, 0)


def test_strip_file_headers() -> None:
    assert strip_file_headers("diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n") == (
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    assert strip_file_headers("@@ -1 +1 @@\n-a\n+b\n") == "@@ -1 +1 @@\n-a\n+b\n"
    assert strip_file_headers("no hunks here") == "no hunks here"


def test_build_patch_is_headerless_and_countable() -> None:
    patch = build_patch("a\nb\nc\n", "a\nB\nc\nd\n")
    assert patch.startswith("@@")
    assert count_changes(patch) == (2, 1)


def test_build_patch_terminates_last_line() -> None:
    patch = build_patch("a", "b")
    assert patch.endswith("\n")
    assert count_changes(patch) == (1, 1)


def test_build_patch_identical_is_empty() -> None:
    assert build_patch("same\n", "same\n") == ""


def test_binary_detection() -> None:
    assert looks_binary(b"PNG\x00\x01")
    assert not looks_binary(b"plain text")
    assert decode_text(b"\x00") is None
    assert decode_text(b"\xff\xfe invalid") is None
    assert decode_text("héllo".encode()) == "héllo"
