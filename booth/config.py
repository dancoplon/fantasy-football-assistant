"""Settings loaded from .env (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Repo root: booth/config.py -> booth/ -> repo
ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str
    token_file: Path
    league_id: str
    game_code: str

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        load_dotenv(env_file or ROOT / ".env")
        missing = [k for k in ("YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET") if not os.getenv(k)]
        if missing:
            raise ConfigError(
                f"Missing {', '.join(missing)}. Copy .env.example to .env and fill them in."
            )
        token_file = Path(os.getenv("YAHOO_TOKEN_FILE", "secrets/yahoo_token.json"))
        if not token_file.is_absolute():
            token_file = ROOT / token_file
        return cls(
            client_id=os.environ["YAHOO_CLIENT_ID"],
            client_secret=os.environ["YAHOO_CLIENT_SECRET"],
            redirect_uri=os.getenv("YAHOO_REDIRECT_URI", "https://localhost:8000"),
            token_file=token_file,
            league_id=os.getenv("YAHOO_LEAGUE_ID", "890283"),
            game_code=os.getenv("YAHOO_GAME_CODE", "nfl"),
        )
