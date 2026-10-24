"""Paths and API keys. Keys come from the environment or the repo's .env (never committed)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOBS_DIR = ROOT / "outputs" / "jobs"


def secret(name):
    value = os.environ.get(name)
    if value:
        return value
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == name:
                value = value.strip().strip('"').strip("'")
                os.environ[name] = value
                return value
    return None
