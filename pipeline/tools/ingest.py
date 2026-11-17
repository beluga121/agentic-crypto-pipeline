"""Ingest step: download the top-100 coin market snapshot from CoinGecko.

The tool saves the raw response to disk and puts only the file path and a
few small numbers into session state. The coin data itself never goes
through the LLM.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.adk.tools import ToolContext

from pipeline import config

API_URL = "https://api.coingecko.com/api/v3/coins/markets"
PARAMS = {
    "vs_currency": "usd",
    "order": "market_cap_desc",
    "per_page": 100,
    "page": 1,
    "price_change_percentage": "24h,7d",
}
USER_AGENT = "crypto-data-pipeline/1.0 (class project; python-requests)"
TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3

# Fields every record must have. If CoinGecko changes its response shape,
# we want to fail here with a clear message, not deep inside pandas.
REQUIRED_FIELDS = (
    "id",
    "symbol",
    "name",
    "current_price",
    "market_cap",
    "total_volume",
    "price_change_percentage_24h",
)


def download_markets(api_key: str = "") -> bytes:
    """Call /coins/markets once and return the raw response body.

    Retries up to MAX_ATTEMPTS times on 429 (rate limit), 5xx and network
    errors, waiting 1 s, then 2 s (exponential backoff). Any other HTTP
    error fails immediately because retrying will not fix it.
    """
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        # Demo keys go in this header. We never print the key itself.
        headers["x-cg-demo-api-key"] = api_key

    last_problem = "unknown error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(API_URL, params=PARAMS, headers=headers, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last_problem = f"network error ({type(exc).__name__})"
        else:
            if response.status_code == 200:
                return response.content
            if response.status_code == 429:
                last_problem = "HTTP 429 rate limit"
            elif response.status_code >= 500:
                last_problem = f"HTTP {response.status_code} server error"
            elif response.status_code in (401, 403):
                raise RuntimeError(
                    f"CoinGecko refused the request (HTTP {response.status_code}). "
                    "Check COINGECKO_API_KEY, or remove it to use the keyless endpoint."
                )
            else:
                raise RuntimeError(f"CoinGecko returned HTTP {response.status_code}.")

        if attempt < MAX_ATTEMPTS:
            time.sleep(2 ** (attempt - 1))

    raise RuntimeError(
        f"CoinGecko request failed after {MAX_ATTEMPTS} attempts: {last_problem}. "
        "If this is a rate limit, wait a minute or add a free Demo key as COINGECKO_API_KEY."
    )


def validate_markets(data: object) -> list[dict]:
    """Check the parsed JSON is a non-empty list of records with the expected fields."""
    if not isinstance(data, list) or not data:
        raise ValueError("CoinGecko response is not a non-empty list.")
    for index, record in enumerate(data):
        if not isinstance(record, dict):
            raise ValueError(f"Record {index} is not a JSON object.")
        missing = [field for field in REQUIRED_FIELDS if field not in record]
        if missing:
            raise ValueError(f"Record {index} is missing fields: {', '.join(missing)}.")
    return data


def save_raw(body: bytes, run_ts: str, raw_dir: Path = config.RAW_DIR) -> Path:
    """Write the response body to disk byte-for-byte, so the raw layer stays untouched."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"coingecko_markets_{run_ts}.json"
    path.write_bytes(body)
    return path


def fetch_market_data(tool_context: ToolContext) -> dict:
    """Fetch the top 100 coins by market cap from CoinGecko and save the raw JSON.

    Takes no arguments. Returns a small status dictionary with the number of
    coins and the local file path.
    """
    # One timestamp per run. Later steps reuse it so every file from this
    # run shares the same name and GCS folder.
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        body = download_markets(config.coingecko_api_key())
        records = validate_markets(json.loads(body.decode("utf-8")))
        path = save_raw(body, run_ts)
    except (RuntimeError, ValueError) as exc:
        tool_context.state["pipeline_error"] = "ingest"
        return {"status": "error", "error_message": config.mask_secrets(exc)}

    tool_context.state["run_ts"] = run_ts
    tool_context.state["raw_path"] = str(path)
    return {"status": "success", "rows": len(records), "path": config.relative(path)}
