"""Runtime configuration, read from environment variables or a .env file."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _num(name: str, default, cast):
    try:
        return cast(_get(name) or default)
    except ValueError:
        return cast(default)


@dataclass
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    user_session: str
    owner_ids: set
    public: bool
    data_dir: Path
    max_concurrent: int
    edit_interval: float
    min_free_gb: float
    quota_cooldown_min: int
    error_cooldown_min: int
    max_attempts: int
    upload_delay: float
    stall_timeout: int
    default_daily_gb: float
    admin_log_chat: int
    log_level: str
    megatools_bin: str = ""
    max_files_on_disk: int = 1

    @property
    def download_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mega_bot.db"


def load() -> Config:
    _load_dotenv(Path(os.environ.get("ENV_FILE", ".env")))
    missing = [k for k in ("API_ID", "API_HASH", "BOT_TOKEN") if not _get(k)]
    if missing:
        raise SystemExit("Missing required settings: %s (see .env.example)" % ", ".join(missing))
    owners = {int(x) for x in re.split(r"[,\s]+", _get("OWNER_IDS")) if x.lstrip("-").isdigit()}
    if not owners:
        raise SystemExit("OWNER_IDS must contain at least one Telegram user ID (see .env.example)")
    cfg = Config(
        api_id=int(_get("API_ID")),
        api_hash=_get("API_HASH"),
        bot_token=_get("BOT_TOKEN"),
        user_session=_get("USER_SESSION"),
        owner_ids=owners,
        public=_get("PUBLIC", "0").lower() in ("1", "true", "yes", "on"),
        data_dir=Path(_get("DATA_DIR", "./data")).resolve(),
        max_concurrent=max(1, _num("MAX_CONCURRENT", 1, int)),
        edit_interval=max(1.5, _num("EDIT_INTERVAL", 3, float)),
        min_free_gb=_num("MIN_FREE_GB", 1, float),
        quota_cooldown_min=_num("QUOTA_COOLDOWN_MIN", 45, int),
        error_cooldown_min=_num("ERROR_COOLDOWN_MIN", 5, int),
        max_attempts=max(1, _num("MAX_ATTEMPTS", 6, int)),
        upload_delay=_num("UPLOAD_DELAY", 1.5, float),
        stall_timeout=max(30, _num("STALL_TIMEOUT", 150, int)),
        default_daily_gb=_num("DEFAULT_DAILY_GB", 0, float),
        admin_log_chat=_num("ADMIN_LOG_CHAT", 0, int),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
        megatools_bin=_get("MEGATOOLS_BIN"),
        max_files_on_disk=max(1, _num("MAX_FILES_ON_DISK", 1, int)),
    )
    for d in (cfg.data_dir, cfg.download_dir, cfg.tmp_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


def secret_key(cfg: Config) -> bytes:
    """Fernet key used to encrypt stored Mega passwords."""
    env = _get("SECRET_KEY")
    if env:
        return env.encode()
    path = cfg.data_dir / "secret.key"
    if path.is_file():
        return path.read_bytes().strip()
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    path.write_bytes(key)
    os.chmod(path, 0o600)
    return key
