"""Path globs with ``**`` support (``PurePath.full_match`` is Python 3.13+).

* ``*`` matches within one path segment, ``**`` across segments, ``?`` one character.
* A pattern without ``/`` matches the file name at any depth (``*.lock`` == ``**/*.lock``).
* Matching is case-insensitive.
"""

import re
from collections.abc import Iterable
from functools import lru_cache


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    if "/" not in pattern:
        pattern = f"**/{pattern}"
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE)


def glob_match(path: str, pattern: str) -> bool:
    return _compile(pattern).fullmatch(path.lstrip("/")) is not None


def match_any(path: str, patterns: Iterable[str]) -> bool:
    return any(glob_match(path, p) for p in patterns)
