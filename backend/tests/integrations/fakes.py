"""In-process fake GitHub and GitLab APIs (``httpx.MockTransport``).

Both fakes serve the *same* change scenario in each provider's own wire format, so the
contract tests can assert that the normalized ``CompareResult`` is identical.

Payload shapes follow the public REST docs (GitHub v3 2022-11-28, GitLab v4). They are
hand-built; recorded fixtures from real accounts can replace them later.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
TOKEN = "s3cr3t-token-value"

GITHUB_API = "https://github.test/api/v3"
GITHUB_REPO = "acme/api"
GITLAB_API = "https://gitlab.test/api/v4"
GITLAB_REPO = "acme/platform/api"  # nested group

# ------------------------------------------------------------------ scenario

COMMITS = [
    ("c1" + "0" * 38, "Add new module", "Ada", "2026-10-01T10:00:00Z"),
    ("c2" + "0" * 38, "Rename helpers", "Grace", "2026-10-02T11:00:00Z"),
    ("c3" + "0" * 38, "Bump big seed file", "Linus", "2026-10-03T12:00:00Z"),
]

PATCH_MODIFIED = "@@ -1,3 +1,4 @@\n import os\n-x = 1\n+x = 2\n+y = 3\n print(x)\n"
PATCH_ADDED = "@@ -0,0 +1,2 @@\n+a = 1\n+b = 2\n"
PATCH_REMOVED = "@@ -1 +0,0 @@\n-gone = True\n"
PATCH_RENAME_EDIT = "@@ -1 +1 @@\n-def f(): pass\n+def g(): pass\n"

BIG_FILE = "data/big.sql"
BIG_OLD = b"".join(b"INSERT INTO t VALUES (%d);\n" % i for i in range(50))
BIG_NEW = BIG_OLD.replace(b"(25);", b"(2500);")

RAW_FILES: dict[tuple[str, str], bytes] = {
    (BIG_FILE, BASE_SHA): BIG_OLD,
    (BIG_FILE, HEAD_SHA): BIG_NEW,
}


@dataclass
class Recorder:
    requests: list[httpx.Request] = field(default_factory=list)

    def paths(self) -> list[str]:
        return [r.url.raw_path.decode().split("?")[0] for r in self.requests]


Handler = Callable[[httpx.Request], httpx.Response]


def _query(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}


def _route(routes: dict[str, Handler], recorder: Recorder) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        path = request.url.raw_path.decode().split("?")[0]
        for prefix, route in routes.items():
            if path == prefix:
                return route(request)
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


# ------------------------------------------------------------------ GitHub


def github_files() -> list[dict[str, Any]]:
    return [
        {"filename": "src/app.py", "status": "modified", "additions": 2, "deletions": 1,
         "changes": 3, "patch": PATCH_MODIFIED},
        {"filename": "src/new.py", "status": "added", "additions": 2, "deletions": 0,
         "changes": 2, "patch": PATCH_ADDED},
        {"filename": "src/old.py", "status": "removed", "additions": 0, "deletions": 1,
         "changes": 1, "patch": PATCH_REMOVED},
        {"filename": "docs/b.md", "previous_filename": "docs/a.md", "status": "renamed",
         "additions": 0, "deletions": 0, "changes": 0},
        {"filename": "src/helpers.py", "previous_filename": "src/util.py", "status": "renamed",
         "additions": 1, "deletions": 1, "changes": 2, "patch": PATCH_RENAME_EDIT},
        # Large diff: GitHub reports counts but omits the patch.
        {"filename": BIG_FILE, "status": "modified", "additions": 1, "deletions": 1,
         "changes": 2},
        # Binary: no patch, no line counts.
        {"filename": "assets/logo.png", "status": "modified", "additions": 0, "deletions": 0,
         "changes": 0},
    ]  # fmt: skip


def github_commit(sha: str, title: str, author: str, date: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "commit": {"message": f"{title}\n\nLonger body.", "author": {"name": author, "date": date}},
    }


def github_handler(
    recorder: Recorder,
    *,
    files: list[dict[str, Any]] | None = None,
    total_commits: int | None = None,
    extra: dict[str, Handler] | None = None,
) -> Handler:
    repo = f"/api/v3/repos/{GITHUB_REPO}"
    compare_path = f"{repo}/compare/{BASE_SHA}...{HEAD_SHA}"
    scenario_files = github_files() if files is None else files
    commits = [github_commit(*c) for c in COMMITS]

    def compare(request: httpx.Request) -> httpx.Response:
        page = int(_query(request).get("page", "1"))
        body: dict[str, Any] = {
            "total_commits": len(commits) if total_commits is None else total_commits,
            "files": scenario_files,  # repeated on page 2: must not be duplicated
        }
        headers = {}
        if page == 1:
            body["commits"] = commits[:2]
            headers["link"] = (
                f"<{GITHUB_API}/repos/{GITHUB_REPO}/compare/{BASE_SHA}...{HEAD_SHA}"
                f'?per_page=100&page=2>; rel="next", <{GITHUB_API}/x?page=2>; rel="last"'
            )
        else:
            body["commits"] = commits[2:]
        return httpx.Response(200, json=body, headers=headers)

    def raw(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"{repo}/contents/")
        content = RAW_FILES.get((path, _query(request)["ref"]))
        return httpx.Response(200, content=content) if content else httpx.Response(404)

    routes: dict[str, Handler] = {
        compare_path: compare,
        f"{repo}/contents/{BIG_FILE}": raw,
        **(extra or {}),
    }
    return _route(routes, recorder)


# ------------------------------------------------------------------ GitLab


def gitlab_diffs() -> list[dict[str, Any]]:
    def diff(old: str, new: str, text: str, **flags: bool) -> dict[str, Any]:
        return {"old_path": old, "new_path": new, "diff": text, "new_file": False,
                "renamed_file": False, "deleted_file": False, **flags}  # fmt: skip

    return [
        diff("src/app.py", "src/app.py", PATCH_MODIFIED),
        diff("src/new.py", "src/new.py", PATCH_ADDED, new_file=True),
        diff("src/old.py", "src/old.py", PATCH_REMOVED, deleted_file=True),
        diff("docs/a.md", "docs/b.md", "", renamed_file=True),
        diff("src/util.py", "src/helpers.py", PATCH_RENAME_EDIT, renamed_file=True),
        # Collapsed: GitLab empties ``diff`` for large files.
        diff(BIG_FILE, BIG_FILE, "", collapsed=True, too_large=False),
        diff("assets/logo.png", "assets/logo.png",
             "Binary files a/assets/logo.png and b/assets/logo.png differ\n"),
    ]  # fmt: skip


def gitlab_handler(
    recorder: Recorder,
    *,
    diffs: list[dict[str, Any]] | None = None,
    compare_timeout: bool = False,
    extra: dict[str, Handler] | None = None,
) -> Handler:
    project = "/api/v4/projects/acme%2Fplatform%2Fapi"
    scenario_diffs = gitlab_diffs() if diffs is None else diffs

    def compare(request: httpx.Request) -> httpx.Response:
        query = _query(request)
        assert query == {"from": BASE_SHA, "to": HEAD_SHA, "straight": "false"}
        commits = [
            {"id": sha, "title": title, "author_name": author, "authored_date": date}
            for sha, title, author, date in COMMITS
        ]
        return httpx.Response(
            200,
            json={"commits": commits, "diffs": scenario_diffs, "compare_timeout": compare_timeout},
        )

    def raw(request: httpx.Request) -> httpx.Response:
        content = RAW_FILES.get((BIG_FILE, _query(request)["ref"]))
        return httpx.Response(200, content=content) if content else httpx.Response(404)

    routes: dict[str, Handler] = {
        f"{project}/repository/compare": compare,
        f"{project}/repository/files/data%2Fbig.sql/raw": raw,
        **(extra or {}),
    }
    return _route(routes, recorder)


def json_route(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> Handler:
    return lambda _request: httpx.Response(status, json=payload, headers=headers)
