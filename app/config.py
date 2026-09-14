import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is installed in normal setup
    load_dotenv = None

if load_dotenv:
    load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str) -> frozenset[str]:
    return frozenset(item.strip().lower() for item in os.getenv(name, default).split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    database_path: str = os.getenv("DATABASE_PATH", "./data/url_shortener.db")
    base_url: str = os.getenv("BASE_URL", "http://localhost:8000")
    short_code_length: int = int(os.getenv("SHORT_CODE_LENGTH", "7"))
    short_code_attempts: int = int(os.getenv("SHORT_CODE_ATTEMPTS", "5"))
    reserved_aliases: frozenset[str] = _csv("RESERVED_ALIASES", "health,docs,redoc,api")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    openai_timeout_seconds: float = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
    max_agent_retries: int = int(os.getenv("MAX_AGENT_RETRIES", "2"))
    parallel_workers: int = int(os.getenv("AGENT_PARALLEL_WORKERS", "4"))
    require_human_approval: bool = _bool("REQUIRE_HUMAN_APPROVAL", True)


settings = Settings()
