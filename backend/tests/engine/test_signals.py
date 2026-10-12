"""Signal detectors across languages and frameworks."""

import pytest

from app.domain.enums import ChangeType
from app.engine.models import Signal, SignalKind
from app.engine.signals import detect
from tests.engine.helpers import added, fd, removed


def kinds(signals: list[Signal], kind: SignalKind) -> set[str | None]:
    return {s.symbol for s in signals if s.kind is kind}


# ---------------------------------------------------------------- env vars


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("app/settings.py", 'key = os.getenv("STRIPE_KEY")'),
        ("app/settings.py", 'key = os.environ["STRIPE_KEY"]'),
        ("app/settings.py", 'key = os.environ.get("STRIPE_KEY", "")'),
        ("src/pay.ts", "const key = process.env.STRIPE_KEY;"),
        ("src/pay.ts", "const key = process.env['STRIPE_KEY'];"),
        ("web/src/pay.ts", "const key = import.meta.env.STRIPE_KEY"),
        ("app/models/pay.rb", 'key = ENV.fetch("STRIPE_KEY")'),
        ("app/models/pay.rb", "key = ENV['STRIPE_KEY']"),
        ("src/Pay.java", 'String key = System.getenv("STRIPE_KEY");'),
        ("pay/pay.go", 'key := os.Getenv("STRIPE_KEY")'),
        ("src/pay.rs", 'let key = env::var("STRIPE_KEY")?;'),
        ("docker-compose.yml", "      STRIPE: ${STRIPE_KEY:-}"),
        (".env.example", "STRIPE_KEY="),
    ],
)
def test_env_var_added(path: str, line: str) -> None:
    assert kinds(detect([fd(path, added(line))]), SignalKind.ENV_VAR_ADDED) == {"STRIPE_KEY"}


def test_template_literal_is_not_an_env_var() -> None:
    signals = detect([fd("src/msg.ts", added("const s = `${API_URL}/users`;"))])
    assert not kinds(signals, SignalKind.ENV_VAR_ADDED)


def test_moved_env_read_is_not_added() -> None:
    signals = detect(
        [
            fd("app/a.py", removed('x = os.getenv("STRIPE_KEY")')),
            fd("app/b.py", added('x = os.getenv("STRIPE_KEY")')),
        ]
    )
    assert not kinds(signals, SignalKind.ENV_VAR_ADDED)
    assert not kinds(signals, SignalKind.ENV_VAR_REMOVED)


def test_env_already_read_in_context_is_not_new() -> None:
    patch = '@@ -1,1 +1,2 @@\n a = os.getenv("STRIPE_KEY")\n+b = os.getenv("STRIPE_KEY")\n'
    assert not kinds(detect([fd("app/a.py", patch)]), SignalKind.ENV_VAR_ADDED)


def test_env_var_removed() -> None:
    signals = detect([fd("app/a.py", removed('x = os.getenv("LEGACY_FLAG")'))])
    assert kinds(signals, SignalKind.ENV_VAR_REMOVED) == {"LEGACY_FLAG"}


# ---------------------------------------------------------------- destructive SQL


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("db/migrations/0042.sql", "ALTER TABLE users DROP COLUMN legacy_email;"),
        ("db/migrations/0042.sql", "DROP TABLE audit_log;"),
        ("db/migrations/0042.sql", "ALTER TABLE users RENAME COLUMN email TO email_address;"),
        ("db/migrations/0042.sql", "ALTER TABLE users ALTER COLUMN age TYPE bigint;"),
        ("db/migrations/0042.sql", "ALTER TABLE users ALTER COLUMN email SET NOT NULL;"),
        ("db/migrations/0042.sql", "DELETE FROM sessions WHERE expired;"),
        ("alembic/versions/0042.py", '    op.drop_column("users", "legacy_email")'),
        (
            "app/migrations/0042_x.py",
            '        migrations.RemoveField(model_name="user", name="x"),',
        ),
        ("db/migrate/20260101_x.rb", "    remove_column :users, :legacy_email"),
        ("migrations/20260101_x.js", "  table.dropColumn('legacy_email');"),
    ],
)
def test_destructive_sql_detected(path: str, line: str) -> None:
    signals = detect([fd(path, added(line), ChangeType.ADDED)])
    assert any(s.kind is SignalKind.DESTRUCTIVE_SQL for s in signals)


@pytest.mark.parametrize(
    "line",
    [
        "ALTER TABLE users ADD COLUMN nickname text;",
        "ALTER TABLE users DROP CONSTRAINT users_email_key;",
        "DROP INDEX ix_users_email;",
        "CREATE INDEX CONCURRENTLY ix ON users (email);",
    ],
)
def test_non_destructive_sql_not_flagged(line: str) -> None:
    signals = detect([fd("db/migrations/0042.sql", added(line))])
    assert not any(s.kind is SignalKind.DESTRUCTIVE_SQL for s in signals)


def test_delete_from_outside_migrations_is_not_destructive_sql() -> None:
    signals = detect(
        [fd("app/repo.py", added('db.execute("DELETE FROM sessions WHERE id = :id")'))]
    )
    assert not any(s.kind is SignalKind.DESTRUCTIVE_SQL for s in signals)


def test_alembic_downgrade_drop_is_ignored() -> None:
    patch = added(
        "def upgrade():",
        '    op.add_column("users", sa.Column("nickname", sa.Text()))',
        "",
        "def downgrade():",
        '    op.drop_column("users", "nickname")',
    )
    signals = detect([fd("alembic/versions/0043_nick.py", patch, ChangeType.ADDED)])
    assert not any(s.kind is SignalKind.DESTRUCTIVE_SQL for s in signals)
    assert kinds(signals, SignalKind.MIGRATION_ADDED) == {"alembic/versions/0043_nick.py"}


def test_downgrade_section_from_hunk_header() -> None:
    patch = '@@ -10,2 +10,3 @@ def downgrade():\n+    op.drop_column("users", "nickname")\n'
    signals = detect([fd("alembic/versions/0043_nick.py", patch)])
    assert not any(s.kind is SignalKind.DESTRUCTIVE_SQL for s in signals)


# ---------------------------------------------------------------- routes


@pytest.mark.parametrize(
    ("path", "line", "symbol"),
    [
        ("app/routers/pay.py", '@router.post("/v1/payments")', "POST /v1/payments"),
        ("app/api.py", '@app.route("/v1/payments")', "/v1/payments"),
        (
            "src/routes/pay.js",
            "router.delete('/v1/payments/:id', handler)",
            "DELETE /v1/payments/:id",
        ),
        ("src/PayController.java", '@GetMapping("/v1/payments")', "GET /v1/payments"),
        ("src/pay.controller.ts", "@Post('refunds')", "POST refunds"),
        ("config/routes.rb", "  get '/v1/payments', to: 'payments#index'", "GET /v1/payments"),
        ("app/urls.py", '    path("v1/payments/", views.list),', "v1/payments/"),
        ("api/routes.go", 'r.GET("/v1/payments", list)', "GET /v1/payments"),
    ],
)
def test_route_removed(path: str, line: str, symbol: str) -> None:
    assert kinds(detect([fd(path, removed(line))]), SignalKind.ROUTE_REMOVED) == {symbol}


def test_route_changed_when_definition_differs() -> None:
    patch = (
        "@@ -1 +1 @@\n"
        '-@router.get("/v1/users", response_model=UserV1)\n'
        '+@router.get("/v1/users", response_model=UserV2)\n'
    )
    signals = detect([fd("app/routers/users.py", patch)])
    assert kinds(signals, SignalKind.ROUTE_CHANGED) == {"GET /v1/users"}
    assert not kinds(signals, SignalKind.ROUTE_REMOVED)


def test_http_client_call_is_not_a_route() -> None:
    signals = detect([fd("web/src/api.ts", removed("const r = await api.get('/v1/users');"))])
    assert not kinds(signals, SignalKind.ROUTE_REMOVED)


# ---------------------------------------------------------------- dependencies


@pytest.mark.parametrize(
    ("path", "line", "name"),
    [
        ("package.json", '    "react": "^19.1.0",', "react"),
        ("package.json", '    "@tanstack/react-query": "5.2.0",', "@tanstack/react-query"),
        ("requirements.txt", "django==5.2.1", "django"),
        ("pyproject.toml", '    "fastapi[standard]>=0.115",', "fastapi"),
        ("go.mod", "\tgithub.com/lib/pq v1.10.9", "github.com/lib/pq"),
        ("Gemfile", "gem 'rails', '~> 8.0'", "rails"),
    ],
)
def test_dependency_changed(path: str, line: str, name: str) -> None:
    assert kinds(detect([fd(path, added(line))]), SignalKind.DEPENDENCY_CHANGED) == {name}


def test_pyproject_metadata_is_not_a_dependency() -> None:
    patch = added('version = "0.2.0"', 'requires-python = ">=3.12"')
    assert not kinds(detect([fd("pyproject.toml", patch)]), SignalKind.DEPENDENCY_CHANGED)
