"""Stage 4: fit files into a model's input budget, most important first."""

import math
from collections.abc import Callable, Sequence

from app.engine.models import AnalysisFile, PackedDiff, SkippedFile, SkipReason
from app.engine.prompt import render_file_block

TokenCounter = Callable[[str], int]

SAFETY_FACTOR = 1.1
MAX_FILE_SHARE = 0.25  # one file may use at most this share of the budget
MIN_USEFUL_TOKENS = 300  # below this a truncated file is not worth including


def approx_counter(text: str) -> int:
    """Provider-independent estimate (~4 chars/token), with the safety factor applied."""
    return math.ceil(len(text) / 4 * SAFETY_FACTOR)


def make_counter(model: str) -> TokenCounter:
    """Model-aware counter via LiteLLM; falls back to the estimate if the model is unknown."""
    import litellm

    def count(text: str) -> int:
        try:
            tokens = int(litellm.token_counter(model=model, text=text))
        except Exception:  # unknown model / tokenizer download failure
            return approx_counter(text)
        return math.ceil(tokens * SAFETY_FACTOR)

    return count


def _hunks(patch: str) -> list[list[str]]:
    hunks: list[list[str]] = []
    for line in patch.splitlines():
        if line.startswith("@@") or not hunks:
            hunks.append([])
        hunks[-1].append(line)
    return hunks


def _marker(omitted_lines: int) -> str:
    return f"[… {omitted_lines} more lines truncated by ReleaseRadar]"


def truncate(
    file: AnalysisFile, limit: int, count: TokenCounter
) -> tuple[AnalysisFile, int] | None:
    """Keep whole hunks from the top (or the head of the first hunk) within ``limit`` tokens."""
    patch = file.diff.patch or ""
    hunks = _hunks(patch)
    total_lines = sum(len(h) for h in hunks)

    def build(lines: list[str]) -> tuple[AnalysisFile, int]:
        text = "\n".join([*lines, _marker(total_lines - len(lines))]) + "\n"
        candidate = file.model_copy(update={"diff": file.diff.model_copy(update={"patch": text})})
        return candidate, count(render_file_block(candidate))

    best: tuple[AnalysisFile, int] | None = None
    kept: list[str] = []
    for hunk in hunks:
        attempt = build([*kept, *hunk])
        if attempt[1] > limit:
            break
        kept, best = [*kept, *hunk], attempt
    if best is not None:
        return best

    # Not even one hunk fits: binary-search how many lines of the first hunk do.
    first = hunks[0] if hunks else []
    low, high = 1, len(first) - 1
    while low <= high:
        mid = (low + high) // 2
        attempt = build(first[:mid])
        if attempt[1] <= limit:
            best, low = attempt, mid + 1
        else:
            high = mid - 1
    return best


def _sort_key(file: AnalysisFile, cost: int) -> tuple[int, int, int]:
    return (int(file.tier), 0 if file.signals else 1, cost)


def pack(
    files: Sequence[AnalysisFile],
    budget: int,
    count: TokenCounter,
    *,
    max_file_share: float = MAX_FILE_SHARE,
) -> PackedDiff:
    """Tier 1 → 2 → 3; within a tier, files with signals first, then smaller first."""
    costs = {id(f): count(render_file_block(f)) for f in files}
    ordered = sorted(files, key=lambda f: _sort_key(f, costs[id(f)]))
    max_file = max(int(budget * max_file_share), MIN_USEFUL_TOKENS)

    remaining = budget
    included: list[AnalysisFile] = []
    skipped: list[SkippedFile] = []
    truncated: list[str] = []
    for file in ordered:
        cost = costs[id(file)]
        limit = min(max_file, remaining)
        if cost <= limit:
            included.append(file)
            remaining -= cost
            continue
        result = truncate(file, limit, count) if limit >= MIN_USEFUL_TOKENS else None
        if result is None:
            skipped.append(SkippedFile(path=file.path, reason=SkipReason.BUDGET, tier=file.tier))
            continue
        shortened, shortened_cost = result
        included.append(shortened)
        truncated.append(file.path)
        remaining -= shortened_cost

    return PackedDiff(
        files=tuple(included),
        skipped=tuple(skipped),
        truncated_paths=tuple(truncated),
        tokens=budget - remaining,
    )
