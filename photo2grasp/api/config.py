"""Paths and API keys. Keys come from the environment or the repo's .env (never committed)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOBS_DIR = ROOT / "outputs" / "jobs"


def load_env():
    """Copy unset keys from the repo `.env` into the environment. A variable already set in the shell wins."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def secret(name):
    load_env()
    return os.environ.get(name)
