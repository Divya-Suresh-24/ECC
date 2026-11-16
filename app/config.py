"""Runtime configuration, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    app_name: str = os.environ.get("APP_NAME", "ECC Protocol Assistant")
    institution: str = os.environ.get("INSTITUTION", "Iowa State University · Lloyd Veterinary Medical Center")

    # Where the database and uploaded files live. Keep this OUTSIDE the git checkout in production.
    data_dir: Path = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve()

    # If the app is served under a sub-path (e.g. https://vetmed.iastate.edu/ecc-assistant), set ROOT_PATH=/ecc-assistant
    root_path: str = os.environ.get("ROOT_PATH", "").rstrip("/")

    # Session cookies
    secret_key: str = os.environ.get("SECRET_KEY", "")
    cookie_secure: bool = _bool("COOKIE_SECURE", False)
    session_hours: int = int(os.environ.get("SESSION_HOURS", "12"))

    # Claude API
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    answer_model: str = os.environ.get("ANSWER_MODEL", "claude-sonnet-5-5")
    rewrite_model: str = os.environ.get("REWRITE_MODEL", "claude-haiku-5-5")
    max_answer_tokens: int = int(os.environ.get("MAX_ANSWER_TOKENS", "1500"))
    # MOCK_LLM=1 runs without an API key: answers are stitched together from retrieved passages (for testing only).
    mock_llm: bool = _bool("MOCK_LLM", False)

    # Retrieval
    top_k: int = int(os.environ.get("TOP_K", "8"))
    chunk_chars: int = int(os.environ.get("CHUNK_CHARS", "1400"))

    # Limits
    max_question_chars: int = int(os.environ.get("MAX_QUESTION_CHARS", "1500"))
    daily_question_limit: int = int(os.environ.get("DAILY_QUESTION_LIMIT", "150"))
    max_upload_mb: int = int(os.environ.get("MAX_UPLOAD_MB", "50"))

    # First-run bootstrap: creates this faculty account if the user table is empty.
    initial_admin_username: str = os.environ.get("INITIAL_ADMIN_USERNAME", "")
    initial_admin_password: str = os.environ.get("INITIAL_ADMIN_PASSWORD", "")

    # Authentication mode. "local" = usernames/passwords stored here.
    # "entra" (Microsoft / ISU NetID single sign-on) is planned; see README → "Moving to NetID sign-in".
    auth_mode: str = os.environ.get("AUTH_MODE", "local")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ecc.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"


settings = Settings()

settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.files_dir.mkdir(parents=True, exist_ok=True)

if not settings.secret_key:
    # Persist a generated key so sessions survive restarts.
    key_file = settings.data_dir / ".secret_key"
    if key_file.exists():
        settings.secret_key = key_file.read_text().strip()
    else:
        settings.secret_key = secrets.token_urlsafe(48)
        key_file.write_text(settings.secret_key)
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
