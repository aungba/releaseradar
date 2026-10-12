"""Stage 3b: deterministic detectors over added/removed lines.

Signals are hints for the LLM *and* evidence for the guardrails. They run across the whole
diff because "added" vs "moved" can only be decided globally (a var read in one file and no
longer read in another was moved, not added).
"""

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from app.domain.enums import ChangeType
from app.engine.globs import match_any
from app.engine.models import Signal, SignalKind
from app.engine.tiering import MANIFESTS, is_migration
from app.integrations.git.base import FileDiff

_MAX_EVIDENCE = 200
_NAME = r"([A-Z][A-Z0-9_]{1,80})"

ENV_PATTERNS = [
    re.compile(rf"""os\.getenv\(\s*["']{_NAME}["']"""),
    re.compile(rf"""os\.environ(?:\.get|\.setdefault|\.pop)?\(\s*["']{_NAME}["']"""),
    re.compile(rf"""os\.environ\[\s*["']{_NAME}["']\s*\]"""),
    re.compile(rf"""process\.env\.{_NAME}\b"""),
    re.compile(rf"""process\.env\[\s*["'`]{_NAME}["'`]\s*\]"""),
    re.compile(rf"""import\.meta\.env\.{_NAME}\b"""),
    re.compile(rf"""Deno\.env\.get\(\s*["']{_NAME}["']"""),
    re.compile(rf"""\bENV(?:\.fetch\(\s*|\[\s*)["']{_NAME}["']"""),  # Ruby
    re.compile(rf"""System\.getenv\(\s*"{_NAME}\""""),  # Java/Kotlin
    re.compile(rf"""os\.(?:Getenv|LookupEnv)\(\s*"{_NAME}\""""),  # Go
    re.compile(rf"""(?:std::)?env::var\(\s*"{_NAME}\""""),  # Rust
    re.compile(rf"""Environment\.GetEnvironmentVariable\(\s*"{_NAME}\""""),  # .NET
    re.compile(rf"""\b(?:env|config)\(\s*["']{_NAME}["']"""),  # Laravel, django-environ
]
# ${VAR} / ${VAR:-default} only in config & shell files (JS template literals look the same).
_SHELL_ENV = re.compile(rf"""\$\{{{_NAME}(?::?[-=?][^}}]*)?\}}""")
_SHELL_ENV_FILES = (
    "*.yml", "*.yaml", "Dockerfile", "Dockerfile.*", "*.dockerfile", "*.sh", "*.bash",
    "*.env", ".env.*", "*.tf", "*.conf", "Procfile", "Makefile",
)  # fmt: skip
_DOTENV_FILES = (".env.example", ".env.sample", ".env.template", ".env.dist", "*.env.example")
_DOTENV_LINE = re.compile(rf"^\s*(?:export\s+)?{_NAME}\s*=")

DESTRUCTIVE_PATTERNS = [
    re.compile(r"\bDROP\s+(?:TABLE|COLUMN|SCHEMA|DATABASE|TYPE)\b", re.I),
    re.compile(r"\bALTER\s+TABLE\b.*\bDROP\b(?!\s+(?:CONSTRAINT|INDEX|DEFAULT))", re.I),
    re.compile(r"\bRENAME\s+(?:COLUMN|TO)\b", re.I),
    re.compile(r"\bALTER\s+COLUMN\b.*\b(?:TYPE|SET\s+NOT\s+NULL)\b", re.I),
    re.compile(r"\bTRUNCATE\s+(?:TABLE\s+)?\w", re.I),
    re.compile(r"\bop\.(?:drop_table|drop_column|alter_column|rename_table)\("),  # Alembic
    re.compile(r"\bmigrations\.(?:RemoveField|DeleteModel|RenameField|RenameModel|AlterField)\("),
    re.compile(r"\b(?:remove_column|drop_table|rename_column|change_column|rename_table)\b"),
    re.compile(r"\.(?:dropColumn|dropTable|renameColumn|dropColumns)\("),  # Knex, TypeORM
]
_DELETE_FROM = re.compile(r"\bDELETE\s+FROM\b", re.I)  # destructive only inside migrations
# Reverse-migration sections: their "drop" lines undo an "add" and are not destructive.
_DOWN_START = re.compile(
    r"\bdef\s+downgrade\b|\bdef\s+down\b|exports\.down\b|\basync\s+down\s*\(|\bdown\s*\(\s*\)\s*\{"
)
_UP_START = re.compile(
    r"\bdef\s+upgrade\b|\bdef\s+up\b|exports\.up\b|\basync\s+up\s*\(|\bup\s*\(\s*\)\s*\{"
)

_ROUTE_PATTERNS: list[tuple[re.Pattern[str], int | None, int]] = [
    # (pattern, method group or None, path group)
    (re.compile(r"""@\w*\.(get|post|put|patch|delete|head|options)\(\s*["']([^"']+)["']"""), 1, 2),
    (re.compile(r"""@\w*\.(?:route|api_route|websocket)\(\s*["']([^"']+)["']"""), None, 1),
    (re.compile(r"""\b(?:app|router|server)\.(get|post|put|patch|delete|all)\(\s*["'`]([^"'`]+)"""), 1, 2),
    (re.compile(r"""@(Get|Post|Put|Patch|Delete)Mapping\(\s*(?:(?:value|path)\s*=\s*)?["']([^"']+)"""), 1, 2),
    (re.compile(r"""@RequestMapping\(\s*(?:(?:value|path)\s*=\s*)?["']([^"']+)"""), None, 1),
    (re.compile(r"""@(Get|Post|Put|Patch|Delete)\(\s*["']([^"']*)["']"""), 1, 2),  # NestJS
    (re.compile(r"""^\s*(get|post|put|patch|delete)\s+["']([^"']+)["']"""), 1, 2),  # Rails
    (re.compile(r"""\b(?:re_)?path\(\s*r?["']([^"']*)["']"""), None, 1),  # Django
    (re.compile(r"""\.(GET|POST|PUT|PATCH|DELETE|HandleFunc|Handle)\(\s*"([^"]+)\""""), 1, 2),  # Go
]  # fmt: skip

_DEP_PATTERNS = [
    re.compile(r"""^\s*"(@?[\w.\-/]+)"\s*:\s*"[~^<>=*]*\s*v?\d"""),  # package.json / composer
    re.compile(r"""^\s*["']?([A-Za-z0-9][A-Za-z0-9_.\-]*)(?:\[[^\]]*\])?\s*(?:==|>=|<=|~=|!=|>|<|\^|~)\s*\d"""),
    re.compile(r"""^\s*(?:require\s+)?([\w.\-]+\.[\w.\-/]+)\s+v\d"""),  # go.mod
    re.compile(r"""^\s*gem\s+["']([\w\-]+)["']"""),  # Gemfile
    re.compile(r"""<artifactId>([\w.\-]+)</artifactId>"""),  # pom.xml
]  # fmt: skip


@dataclass(frozen=True)
class _Line:
    file: FileDiff
    sign: str  # "+", "-" or " "
    text: str
    in_down: bool  # inside a reverse-migration section


def _lines(file: FileDiff) -> Iterator[_Line]:
    if not file.patch:
        return
    in_down = False
    for raw in file.patch.split("\n"):
        if raw.startswith("@@"):
            header = raw.split("@@")[-1]  # function context after the second "@@"
            in_down = bool(_DOWN_START.search(header)) and not _UP_START.search(header)
            continue
        if not raw or raw[0] not in "+- ":
            continue
        text = raw[1:]
        if _DOWN_START.search(text):
            in_down = True
        elif _UP_START.search(text):
            in_down = False
        yield _Line(file, raw[0], text, in_down)


def _evidence(text: str) -> str:
    return text.strip()[:_MAX_EVIDENCE]


def _env_names(line: _Line) -> list[str]:
    names = [m.group(1) for p in ENV_PATTERNS for m in p.finditer(line.text)]
    if match_any(line.file.path, _SHELL_ENV_FILES):
        names += [m.group(1) for m in _SHELL_ENV.finditer(line.text)]
    if match_any(line.file.path, _DOTENV_FILES):
        match = _DOTENV_LINE.match(line.text)
        if match:
            names.append(match.group(1))
    return names


def _routes(text: str) -> list[str]:
    found: list[str] = []
    for pattern, method_group, path_group in _ROUTE_PATTERNS:
        for match in pattern.finditer(text):
            method = match.group(method_group).upper() if method_group else ""
            path = match.group(path_group)
            found.append(f"{method} {path}".strip())
    return found


def detect(files: Sequence[FileDiff]) -> list[Signal]:
    """All signals for the given (already filtered, non-test) files."""
    lines = [line for f in files for line in _lines(f)]
    return [
        *_env_signals(lines),
        *_destructive_signals(lines),
        *_migration_signals(files),
        *_route_signals(lines),
        *_dependency_signals(lines),
    ]


def _env_signals(lines: Iterable[_Line]) -> list[Signal]:
    added: dict[str, _Line] = {}
    removed: dict[str, _Line] = {}
    context: set[str] = set()
    for line in lines:
        for name in _env_names(line):
            if line.sign == "+":
                added.setdefault(name, line)
            elif line.sign == "-":
                removed.setdefault(name, line)
            else:
                context.add(name)
    signals = [
        Signal(kind=SignalKind.ENV_VAR_ADDED, file_path=line.file.path, symbol=name,
               evidence=_evidence(line.text))
        for name, line in added.items()
        if name not in removed and name not in context
    ]  # fmt: skip
    signals += [
        Signal(kind=SignalKind.ENV_VAR_REMOVED, file_path=line.file.path, symbol=name,
               evidence=_evidence(line.text))
        for name, line in removed.items()
        if name not in added and name not in context
    ]  # fmt: skip
    return signals


def _destructive_signals(lines: Iterable[_Line]) -> list[Signal]:
    signals: list[Signal] = []
    seen: set[tuple[str, str]] = set()
    for line in lines:
        if line.sign != "+" or line.in_down:
            continue
        hit = any(p.search(line.text) for p in DESTRUCTIVE_PATTERNS) or (
            is_migration(line.file.path) and _DELETE_FROM.search(line.text)
        )
        key = (line.file.path, _evidence(line.text))
        if hit and key not in seen:
            seen.add(key)
            signals.append(
                Signal(kind=SignalKind.DESTRUCTIVE_SQL, file_path=line.file.path,
                       evidence=_evidence(line.text))
            )  # fmt: skip
    return signals


def _migration_signals(files: Iterable[FileDiff]) -> list[Signal]:
    return [
        Signal(kind=SignalKind.MIGRATION_ADDED, file_path=f.path, symbol=f.path)
        for f in files
        if f.status is ChangeType.ADDED and is_migration(f.path)
    ]


def _route_signals(lines: Iterable[_Line]) -> list[Signal]:
    added: dict[str, str] = {}
    removed: dict[str, _Line] = {}
    for line in lines:
        if line.sign == " ":
            continue
        for route in _routes(line.text):
            if line.sign == "+":
                added.setdefault(route, line.text.strip())
            else:
                removed.setdefault(route, line)
    signals: list[Signal] = []
    for route, line in removed.items():
        if route not in added:
            kind = SignalKind.ROUTE_REMOVED
        elif added[route] != line.text.strip():
            kind = SignalKind.ROUTE_CHANGED
        else:
            continue  # moved unchanged
        signals.append(
            Signal(kind=kind, file_path=line.file.path, symbol=route, evidence=_evidence(line.text))
        )
    return signals


def _dependency_signals(lines: Iterable[_Line]) -> list[Signal]:
    signals: dict[tuple[str, str], Signal] = {}
    for line in lines:
        if line.sign == " " or not match_any(line.file.path, MANIFESTS):
            continue
        for pattern in _DEP_PATTERNS:
            match = pattern.search(line.text)
            if match:
                key = (line.file.path, match.group(1))
                signals.setdefault(
                    key,
                    Signal(kind=SignalKind.DEPENDENCY_CHANGED, file_path=line.file.path,
                           symbol=match.group(1), evidence=_evidence(line.text)),
                )  # fmt: skip
                break
    return list(signals.values())
