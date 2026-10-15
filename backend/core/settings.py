"""
core/settings.py

Loads all configuration at startup from environment variables and YAML
config files. Validates that every required value is present before the
application accepts any requests — a missing setting is caught immediately,
not discovered mid-request.

All other modules import from here. Nothing reads os.environ directly.
"""

from dotenv import load_dotenv
load_dotenv()  # read .env before any env var is accessed

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


CONFIG_DIR = Path(__file__).parent.parent / "config"


@dataclass
class LLMSettings:
    api_url: str
    api_key: str
    model: str
    max_retries: int
    timeout_per_step: int
    aws_secret: Optional[str]
    aws_region: Optional[str]
    langfuse_url: Optional[str]
    langfuse_api_key: Optional[str]
    provider_type: str = "gemini"
    azure_api_version: Optional[str] = None
    azure_deployment_name: Optional[str] = None


@dataclass
class DatabaseSettings:
    url: str


@dataclass
class AppSettings:
    port: int
    cors_origins: list[str]
    selenium_headed: bool


@dataclass
class JiraSettings:
    """
    Jira connection settings for the Jira source adapter.

    Optional: the credentials are read with _optional_env so the application
    still starts when Jira is not configured. The adapter raises a clear error
    only if a Jira-sourced run is actually attempted without them.
    """
    base_url: str
    email: str
    api_token: str
    project_key: str
    issue_type: str


@dataclass
class ALMSettings:
    """
    ALM connection settings for read-only requirements ingestion.

    base_url is optional at startup so environments that do not use ALM are
    not blocked. The ALM routes require either this setting or a request-time
    override.
    """
    base_url: str
    connect_timeout_sec: int
    read_timeout_sec: int
    max_retries: int
    retry_backoff_base_ms: int
    ssl_verify: bool


@dataclass
class HelpSettings:
    """
    Help & Support behaviour.

    search_strategy selects which implementation in service/help_search/
    answers a query. It is a setting rather than a code path so retrieval can
    move from keyword matching to embeddings without any caller changing:
    everything above SearchStrategy.search() is identical either way.

    Attributes:
        search_strategy: Key into service.help_search.registry.STRATEGIES.
            'fts' is Postgres full-text search over help_chunks — the only
            strategy implemented today. A vector or hybrid strategy plugs in
            here later without touching the routes, the chat tool or the UI.
        min_score: Absolute relevance floor. Zero for full-text search,
            because ts_rank_cd is a cover-density measure rather than a
            calibrated score: a precise all-terms match can score far LOWER
            than a loose any-term one, so any fixed threshold discards good
            results. Retained because a vector strategy's cosine similarity IS
            calibrated 0-1, and there an absolute floor is the right tool.
        relative_floor: Fraction of the best result's score, within the same
            retrieval pass, below which a passage is treated as noise. Within
            one pass the scores ARE comparable, so this is the filter that
            actually works for full-text search. It matters because a local
            model given two good passages and one irrelevant one answers worse
            than one given two. 0.0 disables it.
        max_passages: How many passages are returned to the model. Kept small
            deliberately — a local model given ten passages answers worse than
            one given three.
        content_dir: Where the authored markdown articles live, relative to
            the backend directory. Article text is authored in files and
            seeded into the database; it is never embedded in Python.
        training_dir: Where uploaded training documents are stored, relative
            to the backend directory. Unlike a BRD — which is read once during
            a survey and can live in a temp directory — a training document
            stays downloadable for as long as the training record referencing
            it must remain readable, so it needs a persistent home.
        chunk_target_words: Approximate size of a retrievable passage. Chunks
            are created at write time and are the unit a future vector store
            would embed, so this value shapes retrieval quality later as well
            as now.
    """
    search_strategy: str
    min_score: float
    relative_floor: float
    max_passages: int
    content_dir: str
    training_dir: str
    chunk_target_words: int


@dataclass
class TicketingSettings:
    """
    Where a support ticket raised inside NextGen2 is created.

    provider names an implementation in service/ticketing/registry.py. NextGen2
    itself never references a specific ticketing system: moving from Jira to
    ServiceNow is a new provider class plus this one value, with no schema
    change and no change to any route or screen.

    The Jira fields here are deliberately separate from JiraSettings above.
    That block is the requirements SOURCE (read-only, a different project);
    this is the support DESTINATION (write). Pointing both at one project
    would let support tickets be picked up by the requirements sync as if they
    were requirements in NextGen2.

    Attributes:
        provider: Key into service.ticketing.registry.PROVIDERS, e.g. 'jira'.
        jira_project_key: Jira project support tickets are created in. Must
            NOT be the requirements project.
        jira_issue_type: Issue type to create, e.g. 'Task' or 'Bug'.
    """
    provider: str
    jira_project_key: str
    jira_issue_type: str


@dataclass
class Settings:
    llm: LLMSettings
    db: DatabaseSettings
    app: AppSettings
    jira: JiraSettings
    alm: ALMSettings
    help: HelpSettings
    ticketing: TicketingSettings
    applications_config: dict
    import_aliases: dict


def _require_env(name: str) -> str:
    """Reads a required environment variable. Raises clearly if missing."""
    value = os.environ.get(name)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            f"Copy .env.example to .env and fill in the value."
        )
    return value


def _optional_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _load_yaml(filename: str) -> dict:
    path = CONFIG_DIR / filename
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_settings() -> Settings:
    """
    Loads and validates all configuration.
    Called once at startup in api/main.py.
    Raises EnvironmentError with a clear message if anything is missing.
    """
    applications_config = _load_yaml("applications.yaml")
    import_aliases = _load_yaml("import_aliases.yaml")

    llm = LLMSettings(
        api_url=_require_env("LLM_API_URL"),
        api_key=_require_env("LLM_API_KEY"),
        model=_require_env("LLM_MODEL"),
        max_retries=int(_optional_env("LLM_MAX_RETRIES", "3")),
        timeout_per_step=int(_optional_env("LLM_TIMEOUT_PER_STEP", "180")),
        aws_secret=_optional_env("LLM_AWS_SECRET") or None,
        aws_region=_optional_env("LLM_AWS_REGION", "us-east-1"),
        langfuse_url=_optional_env("LANGFUSE_URL") or None,
        langfuse_api_key=_optional_env("LANGFUSE_API_KEY") or None,
        provider_type=_optional_env("LLM_PROVIDER_TYPE", "gemini").lower(),
        azure_api_version=_optional_env("AZURE_OPENAI_API_VERSION") or None,
        azure_deployment_name=_optional_env("AZURE_OPENAI_DEPLOYMENT_NAME") or None,
    )

    db = DatabaseSettings(url=_require_env("DATABASE_URL"))

    cors_raw = _optional_env("CORS_ORIGINS", "http://localhost:9000")
    cors_origins = [o.strip() for o in cors_raw.split(",") if o.strip()]

    app = AppSettings(
        port=int(_optional_env("APP_PORT", "9090")),
        cors_origins=cors_origins,
        selenium_headed=_optional_env("SELENIUM_HEADED", "true").lower() == "true",
    )

    jira = JiraSettings(
        base_url=_optional_env("JIRA_BASE_URL").rstrip("/"),
        email=_optional_env("JIRA_EMAIL"),
        api_token=_optional_env("JIRA_API_TOKEN"),
        project_key=_optional_env("JIRA_PROJECT_KEY", "KAN"),
        issue_type=_optional_env("JIRA_ISSUE_TYPE", "Feature"),
    )

    alm = ALMSettings(
        base_url=_optional_env("ALM_BASE_URL", "https://baxter.alm.saas.microfocus.com/qcbin").rstrip("/"),
        connect_timeout_sec=int(_optional_env("ALM_CONNECT_TIMEOUT_SEC", "10")),
        read_timeout_sec=int(_optional_env("ALM_READ_TIMEOUT_SEC", "30")),
        max_retries=int(_optional_env("ALM_MAX_RETRIES", "3")),
        retry_backoff_base_ms=int(_optional_env("ALM_RETRY_BACKOFF_BASE_MS", "250")),
        ssl_verify=_optional_env("ALM_SSL_VERIFY", "true").lower() == "true",
    )

    help_settings = HelpSettings(
        search_strategy=_optional_env("HELP_SEARCH_STRATEGY", "fts").lower(),
        min_score=float(_optional_env("HELP_MIN_SCORE", "0.0")),
        relative_floor=float(_optional_env("HELP_RELATIVE_FLOOR", "0.2")),
        max_passages=int(_optional_env("HELP_MAX_PASSAGES", "3")),
        content_dir=_optional_env("HELP_CONTENT_DIR", "content/help"),
        training_dir=_optional_env("HELP_TRAINING_DIR", "storage/training"),
        chunk_target_words=int(_optional_env("HELP_CHUNK_TARGET_WORDS", "300")),
    )

    ticketing = TicketingSettings(
        provider=_optional_env("TICKETING_PROVIDER", "jira").lower(),
        # No default project key on purpose. Falling back to the requirements
        # project would silently file support tickets into the project the
        # sync engine reads requirements from. The provider raises a clear
        # error if this is unset when a ticket is actually raised.
        jira_project_key=_optional_env("JIRA_SUPPORT_PROJECT_KEY"),
        jira_issue_type=_optional_env("JIRA_SUPPORT_ISSUE_TYPE", "Task"),
    )

    return Settings(
        llm=llm,
        db=db,
        app=app,
        jira=jira,
        alm=alm,
        help=help_settings,
        ticketing=ticketing,
        applications_config=applications_config,
        import_aliases=import_aliases,
    )


# Singleton — loaded once, imported everywhere
settings: Settings = load_settings()