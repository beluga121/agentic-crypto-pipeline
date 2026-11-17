"""Configuration: load `.env`, validate it, and keep secrets out of output.

Every other module reads settings through the small helpers in this file,
so there is exactly one place that knows about environment variables.
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

# Project root = the folder that contains main.py. Using pathlib keeps every
# path working the same way on Windows, macOS and Linux.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
RAW_DIR = OUTPUT_DIR / "raw"
PROCESSED_DIR = OUTPUT_DIR / "processed"
REPORT_DIR = OUTPUT_DIR / "report"
GCS_MIRROR_DIR = OUTPUT_DIR / "gcs_mirror"

# A current, stable Gemini Flash model (checked November 2026).
# Override it with GEMINI_MODEL in .env if Google retires it.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

# Load .env once, when this module is first imported. Values that are already
# set in the real environment win, which is the usual convention.
load_dotenv(PROJECT_ROOT / ".env", encoding="utf-8")

# We use an AI Studio API key, not Vertex AI. Setting this default here means
# the user does not have to remember it in .env.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

# GCS bucket naming rules (simplified): 3-63 chars, lowercase letters,
# digits, dashes, underscores and dots; must start and end with a letter
# or digit. Names with dots (domain buckets) are allowed by GCS but are
# rejected here to keep things simple.
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]$")

# Patterns that look like credentials. Used to mask anything we print.
_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),  # Google API keys
    re.compile(r"CG-[0-9A-Za-z]{10,}"),  # CoinGecko demo keys
]


def relative(path) -> str:
    """Show a path relative to the project root (shorter for console and LLM)."""
    try:
        return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def gemini_model() -> str:
    """Return the Gemini model name from GEMINI_MODEL, or the default."""
    return os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL


def coingecko_api_key() -> str:
    """Return the optional CoinGecko Demo API key ("" if not set)."""
    return os.getenv("COINGECKO_API_KEY", "").strip()


def gcs_bucket() -> str:
    """Return the GCS bucket name from GCS_BUCKET ("" if not set)."""
    return os.getenv("GCS_BUCKET", "").strip()


def gcp_project() -> str | None:
    """Return GOOGLE_CLOUD_PROJECT if set; the storage client can also infer it."""
    return os.getenv("GOOGLE_CLOUD_PROJECT", "").strip() or None


def storage_mode() -> str:
    """Return "local" or "gcs". main.py sets STORAGE_MODE=local for --local-only."""
    mode = os.getenv("STORAGE_MODE", "gcs").strip().lower()
    return "local" if mode == "local" else "gcs"


def validate(local_only: bool) -> list[str]:
    """Check required settings and return a list of friendly error messages.

    An empty list means the configuration is usable.
    """
    errors = []

    key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not key or key.startswith("your-"):
        errors.append(
            "GOOGLE_API_KEY is missing. Create a key at https://aistudio.google.com/apikey "
            "and put it in your .env file."
        )

    if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() not in ("FALSE", "0"):
        errors.append("GOOGLE_GENAI_USE_VERTEXAI must be FALSE (this project uses an AI Studio key).")

    if not local_only:
        bucket = gcs_bucket()
        if not bucket or bucket.startswith("your-"):
            errors.append(
                "GCS_BUCKET is missing. Set it in .env, or run with --local-only to skip GCS."
            )
        elif not _BUCKET_RE.match(bucket) or bucket.startswith("goog") or "google" in bucket:
            errors.append(
                f"GCS_BUCKET '{bucket}' is not a valid bucket name. Use 3-63 lowercase "
                "letters, digits, dashes or underscores, starting and ending with a letter or digit."
            )

    return errors


def mask_secrets(text: str) -> str:
    """Replace anything that looks like a credential with a masked version.

    Used on every message we print, so a key that sneaks into an exception
    string never reaches the console.
    """
    text = str(text)
    for name in ("GOOGLE_API_KEY", "COINGECKO_API_KEY"):
        value = os.getenv(name, "").strip()
        if len(value) >= 8:
            text = text.replace(value, value[:4] + "****")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(0)[:4] + "****", text)
    return text
