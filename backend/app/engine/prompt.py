"""Stage 5: render the versioned prompt. Bumping ``PROMPT_VERSION`` requires new eval results."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.engine.models import AnalysisFile, PackedDiff, ReviewContext, Signal, SkippedFile
from app.schemas.llm_contract import ReleaseReviewReport

PROMPT_VERSION = "v1"
MAX_COMMITS = 50
MAX_SIGNALS = 100
MAX_NOT_SHOWN = 200
_PROMPTS_DIR = Path(__file__).parent / "prompts"

Message = dict[str, str]


@dataclass(frozen=True)
class DiffStats:
    files_total: int
    additions: int
    deletions: int


@lru_cache
def _env(version: str) -> Environment:
    return Environment(
        loader=FileSystemLoader(_PROMPTS_DIR / version),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - plain-text prompts, not HTML
        keep_trailing_newline=True,
        trim_blocks=False,
    )


@lru_cache
def report_schema() -> dict[str, object]:
    return ReleaseReviewReport.model_json_schema()


@lru_cache
def system_prompt(version: str = PROMPT_VERSION) -> str:
    schema = json.dumps(report_schema(), separators=(",", ":"))
    return _env(version).get_template("system.md").render(schema=schema)


def _neutralize(text: str) -> str:
    """Stop repository content from closing our delimiters."""
    return text.replace("</diff", "<\\/diff").replace("</file", "<\\/file")


def render_file_block(file: AnalysisFile, patch: str | None = None) -> str:
    body = file.diff.patch if patch is None else patch
    attrs = f'path="{file.path}" status="{file.diff.status}" tier="{int(file.tier)}"'
    if file.diff.old_path:
        attrs += f' old_path="{file.diff.old_path}"'
    if file.signals:
        attrs += f' signals="{",".join(sorted({s.kind.value for s in file.signals}))}"'
    content = _neutralize(body or "(no content change)").rstrip("\n")
    return f"<file {attrs}>\n{content}\n</file>"


def build_messages(
    context: ReviewContext,
    packed: PackedDiff,
    *,
    stats: DiffStats,
    signals: Sequence[Signal] = (),
    not_shown: Sequence[SkippedFile] = (),
    provider_truncated: bool = False,
    version: str = PROMPT_VERSION,
) -> list[Message]:
    commits = context.commits[:MAX_COMMITS]
    user = (
        _env(version)
        .get_template("user.md.j2")
        .render(
            context=context,
            stats=stats,
            commits=commits,
            commits_omitted=max(0, len(context.commits) - len(commits)),
            signals=list(signals)[:MAX_SIGNALS],
            not_shown=[*not_shown, *packed.skipped][:MAX_NOT_SHOWN],
            truncated=packed.truncated_paths,
            provider_truncated=provider_truncated,
            files="\n".join(render_file_block(f) for f in packed.files),
        )
    )
    return [
        {"role": "system", "content": system_prompt(version)},
        {"role": "user", "content": user},
    ]
