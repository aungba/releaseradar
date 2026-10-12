"""Stage 3a: assign a priority tier from the file path (signals may promote later)."""

from app.engine.globs import match_any
from app.engine.models import PROMOTING_SIGNALS, Signal, Tier

MIGRATIONS = (
    "**/migrations/**", "**/migrate/**", "alembic/versions/**", "**/alembic/versions/**",
    "*.sql", "**/flyway/**", "**/liquibase/**", "*changelog*.xml", "*changelog*.yaml",
    "prisma/schema.prisma", "**/prisma/schema.prisma", "**/prisma/migrations/**",
)  # fmt: skip
CONTRACTS = (
    "*.proto", "*.graphql", "*.graphqls", "*.gql", "openapi.*", "swagger.*", "*.avsc",
    "schema.prisma", "**/schemas/**/*.json",
)  # fmt: skip
ROUTERS = (
    "**/routes/**", "**/routers/**", "**/router/**", "routes.*", "router.*", "urls.py",
    "**/urls.py", "config/routes.rb", "**/api/**/endpoints/**",
)  # fmt: skip
CONFIG_INFRA = (
    ".env.example", ".env.sample", ".env.template", ".env.dist", "settings.py", "settings/*.py",
    "**/settings/*.py", "config.py", "**/config/**", "config/**", "Dockerfile", "Dockerfile.*",
    "*.dockerfile", "docker-compose*.yml", "docker-compose*.yaml", "compose.yaml", "compose.yml",
    "**/helm/**", "**/charts/**", "**/k8s/**", "**/kubernetes/**", "*.tf", "*.tfvars",
    "Procfile", "nginx*.conf", "**/.github/workflows/**", ".gitlab-ci.yml", "serverless.yml",
    "app.yaml", "application.yml", "application.yaml", "application.properties",
)  # fmt: skip
MANIFESTS = (
    "package.json", "pyproject.toml", "requirements*.txt", "setup.py", "setup.cfg", "Pipfile",
    "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "Cargo.toml",
    "composer.json", "*.csproj", "mix.exs", "pubspec.yaml",
)  # fmt: skip
SECURITY = (
    "**/auth/**", "**/authentication/**", "**/authorization/**", "*auth*.py", "*auth*.ts",
    "*auth*.js", "*auth*.go", "*auth*.java", "*auth*.rb", "**/permissions*", "**/middleware*",
    "**/security/**", "**/guards/**", "**/policies/**", "*cors*",
)  # fmt: skip
TIER1: tuple[str, ...] = MIGRATIONS + CONTRACTS + ROUTERS + CONFIG_INFRA + MANIFESTS + SECURITY

TIER3: tuple[str, ...] = (
    "tests/**", "test/**", "**/tests/**", "**/test/**", "**/__tests__/**", "spec/**",
    "**/spec/**", "test_*.py", "*_test.py", "*_test.go", "*.test.*", "*.spec.*", "*Test.java",
    "*Tests.java", "*_spec.rb", "conftest.py", "**/fixtures/**", "**/testdata/**",
    "*.md", "*.rst", "*.txt", "*.adoc", "docs/**", "**/docs/**", "LICENSE*", "CHANGELOG*",
    "*.css", "*.scss", "*.sass", "*.less", "*.styl", "*.stories.*", "**/storybook/**",
    ".editorconfig", ".prettierrc*", ".eslintrc*", ".gitignore", ".gitattributes",
)  # fmt: skip


def is_migration(path: str) -> bool:
    return match_any(path, MIGRATIONS)


def path_tier(path: str) -> Tier:
    """Tier from path alone. Test/doc rules win, so ``tests/migrations/x.py`` stays Tier 3,
    except requirement manifests named ``requirements*.txt`` which are Tier 1."""
    if match_any(path, MANIFESTS):
        return Tier.CRITICAL
    if match_any(path, TIER3):
        return Tier.LOW
    if match_any(path, TIER1):
        return Tier.CRITICAL
    return Tier.CORE


def promote(tier: Tier, signals: tuple[Signal, ...]) -> Tier:
    """Files with strong signals are analyzed first, whatever their path says."""
    if tier is Tier.CORE and any(s.kind in PROMOTING_SIGNALS for s in signals):
        return Tier.CRITICAL
    return tier
