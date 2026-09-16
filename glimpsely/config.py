"""Configuration: .env overrides + sane defaults."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


@dataclass
class Config:
    omlx_base_url: str = "http://127.0.0.1:8000"
    omlx_api_key: str = "1234"
    omlx_model: str = "Qwen3.5-9B-4bit"
    db_path: Path = field(default_factory=lambda: Path("data/glimpsely.db"))
    media_dir: Path = field(default_factory=lambda: Path("data/media"))
    digest_hour: int = 21
    digest_minute: int = 0
    memory_ttl_days: int = 3
    push_limit_per_hour: int = 3
    push_daily_cap: int = 15
    llm_max_tokens: int = 800
    llm_temperature: float = 0.1
    llm_timeout: float = 180.0
    asr_model: Path = field(default_factory=lambda: Path("models/sensevoice/model.int8.onnx"))
    asr_tokens: Path = field(default_factory=lambda: Path("models/sensevoice/tokens.txt"))

    @classmethod
    def load(cls, root: Path | None = None) -> "Config":
        root = root or Path.cwd()
        _load_env(root / ".env")
        c = cls()
        c.omlx_base_url = os.environ.get("OMLX_BASE_URL", c.omlx_base_url)
        c.omlx_api_key = os.environ.get("OMLX_API_KEY", c.omlx_api_key)
        c.omlx_model = os.environ.get("OMLX_MODEL", c.omlx_model)
        c.db_path = Path(os.environ.get("DB_PATH", str(c.db_path)))
        c.media_dir = Path(os.environ.get("MEDIA_DIR", str(c.media_dir)))
        c.digest_hour = int(os.environ.get("DIGEST_HOUR", c.digest_hour))
        c.digest_minute = int(os.environ.get("DIGEST_MINUTE", c.digest_minute))
        c.memory_ttl_days = int(os.environ.get("MEMORY_TTL_DAYS", c.memory_ttl_days))
        c.push_limit_per_hour = int(os.environ.get("PUSH_LIMIT_PER_HOUR", c.push_limit_per_hour))
        c.push_daily_cap = int(os.environ.get("PUSH_DAILY_CAP", c.push_daily_cap))
        c.llm_max_tokens = int(os.environ.get("LLM_MAX_TOKENS", c.llm_max_tokens))
        c.llm_temperature = float(os.environ.get("LLM_TEMPERATURE", c.llm_temperature))
        c.llm_timeout = float(os.environ.get("LLM_TIMEOUT", c.llm_timeout))
        c.asr_model = Path(os.environ.get("ASR_MODEL", str(c.asr_model)))
        c.asr_tokens = Path(os.environ.get("ASR_TOKENS", str(c.asr_tokens)))
        return c

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
