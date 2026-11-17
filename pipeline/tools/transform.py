"""Transform step: clean, filter, enrich and aggregate the raw snapshot with pandas.

Each transformation is a small pure function (DataFrame in, DataFrame out),
so it can be unit-tested without the network, the LLM or GCS. The ADK tool
at the bottom just chains them together and writes the results to disk.
"""

import json
from pathlib import Path

import pandas as pd
from google.adk.tools import ToolContext

from pipeline import config

# Columns we keep from the API, renamed to shorter, clearer names.
COLUMN_MAP = {
    "market_cap_rank": "rank",
    "id": "id",
    "symbol": "symbol",
    "name": "name",
    "current_price": "price_usd",
    "market_cap": "market_cap_usd",
    "total_volume": "volume_24h_usd",
    "price_change_percentage_24h": "change_24h_pct",
    "price_change_percentage_7d_in_currency": "change_7d_pct",
}
NUMERIC_COLUMNS = ["rank", "price_usd", "market_cap_usd", "volume_24h_usd", "change_24h_pct", "change_7d_pct"]
TEXT_COLUMNS = ["id", "symbol", "name"]

# Well-known USD stablecoins. A stablecoin's price is pegged to $1, so its
# "24h change" is noise, and its large market cap would distort tier shares
# and the weighted market change. We exclude them by ID or symbol.
KNOWN_STABLECOIN_IDS = {
    "tether", "usd-coin", "dai", "first-digital-usd", "true-usd", "paypal-usd",
    "ethena-usde", "usds", "frax", "pax-dollar", "gemini-dollar", "usdd",
    "binance-usd", "paxos-standard", "ripple-usd", "global-dollar", "usd1-wlfi",
}
KNOWN_STABLECOIN_SYMBOLS = {
    "usdt", "usdc", "dai", "fdusd", "tusd", "pyusd", "usde", "usds", "frax",
    "usdp", "gusd", "usdd", "busd", "rlusd", "usdg", "usd1",
}

# Rule for stablecoins not on the list: a coin trading within 2 cents of $1
# AND moving less than 0.5% in 24h is behaving like a dollar peg. Ordinary
# coins almost never satisfy both at once, so false positives are rare.
PEG_PRICE_MIN, PEG_PRICE_MAX = 0.98, 1.02
PEG_MAX_ABS_CHANGE_PCT = 0.5

# Market-cap tiers, largest first. Lower bound in USD.
TIERS = [("Mega", 100e9), ("Large", 10e9), ("Mid", 1e9), ("Small", 0.0)]
TIER_ORDER = [name for name, _ in TIERS]


def load_raw(path: Path) -> pd.DataFrame:
    """Read the raw CoinGecko JSON file into a DataFrame."""
    with open(path, encoding="utf-8") as handle:
        return pd.DataFrame(json.load(handle))


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Keep the needed columns, coerce types, and drop unusable rows.

    API data is untrusted: anything that is not a number becomes NaN, and
    rows without a price or a positive market cap are dropped because every
    later step depends on them.
    """
    out = df.reindex(columns=list(COLUMN_MAP)).rename(columns=COLUMN_MAP)
    for column in NUMERIC_COLUMNS:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in TEXT_COLUMNS:
        out[column] = out[column].fillna("").astype(str).str.strip()
    out["symbol"] = out["symbol"].str.upper()
    out = out.dropna(subset=["price_usd", "market_cap_usd"])
    out = out[out["market_cap_usd"] > 0]
    return out.reset_index(drop=True)


def is_stablecoin(df: pd.DataFrame) -> pd.Series:
    """Return a True/False Series marking stablecoins (known list OR peg rule)."""
    known = df["id"].str.lower().isin(KNOWN_STABLECOIN_IDS) | df["symbol"].str.lower().isin(
        KNOWN_STABLECOIN_SYMBOLS
    )
    pegged = (
        df["price_usd"].between(PEG_PRICE_MIN, PEG_PRICE_MAX)
        & df["change_24h_pct"].abs().le(PEG_MAX_ABS_CHANGE_PCT)
    )
    return known | pegged


def filter_stablecoins(df: pd.DataFrame) -> pd.DataFrame:
    """Drop stablecoins so the analysis reflects price-moving assets only."""
    return df[~is_stablecoin(df)].reset_index(drop=True)


def market_cap_tier(market_cap_usd: float) -> str:
    """Map a market cap in USD to Mega / Large / Mid / Small."""
    for name, lower_bound in TIERS:
        if market_cap_usd >= lower_bound:
            return name
    return "Small"


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Add the liquidity ratio and the market-cap tier.

    volume_to_mcap_ratio = 24h volume / market cap. A high ratio means the
    coin trades a lot relative to its size (liquid, or heavily speculated).
    """
    out = df.copy()
    out["volume_to_mcap_ratio"] = (out["volume_24h_usd"] / out["market_cap_usd"]).round(4)
    out["market_cap_tier"] = out["market_cap_usd"].map(market_cap_tier)
    return out


def summarize_by_tier(df: pd.DataFrame) -> pd.DataFrame:
    """Group by tier: count, total cap, share of cap, median/mean 24h change, volume."""
    grouped = df.groupby("market_cap_tier").agg(
        coin_count=("id", "count"),
        total_market_cap_usd=("market_cap_usd", "sum"),
        median_change_24h_pct=("change_24h_pct", "median"),
        mean_change_24h_pct=("change_24h_pct", "mean"),
        total_volume_24h_usd=("volume_24h_usd", "sum"),
    )
    # Keep tiers in size order and drop tiers that have no coins.
    grouped = grouped.reindex(TIER_ORDER).dropna(subset=["coin_count"])
    grouped["coin_count"] = grouped["coin_count"].astype(int)
    grouped["market_cap_share_pct"] = 100 * grouped["total_market_cap_usd"] / df["market_cap_usd"].sum()
    grouped = grouped.round({"market_cap_share_pct": 2, "median_change_24h_pct": 2, "mean_change_24h_pct": 2})
    columns = [
        "coin_count", "total_market_cap_usd", "market_cap_share_pct",
        "median_change_24h_pct", "mean_change_24h_pct", "total_volume_24h_usd",
    ]
    return grouped[columns].rename_axis("tier").reset_index()


def top_movers(df: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """Return the top n gainers (change > 0) and top n losers (change < 0) by 24h change.

    Requiring a positive/negative change means a "loser" is never a coin
    that actually went up, even on a day when the whole market rises.
    """
    columns = ["id", "symbol", "name", "price_usd", "market_cap_usd", "change_24h_pct"]
    gainers = df[df["change_24h_pct"] > 0].nlargest(n, "change_24h_pct")[columns].assign(direction="gainer")
    losers = df[df["change_24h_pct"] < 0].nsmallest(n, "change_24h_pct")[columns].assign(direction="loser")
    for part in (gainers, losers):
        part.insert(0, "position", range(1, len(part) + 1))
    movers = pd.concat([gainers, losers], ignore_index=True)
    return movers[["direction", "position"] + columns]


def compute_kpis(df: pd.DataFrame, stablecoins_excluded: int = 0) -> dict:
    """Headline numbers for the report.

    The weighted 24h change weights each coin by market cap, so it shows
    how the market as a whole moved (Bitcoin counts more than a tiny coin).
    """
    with_change = df.dropna(subset=["change_24h_pct"])
    weight = with_change["market_cap_usd"].sum()
    weighted = (with_change["market_cap_usd"] * with_change["change_24h_pct"]).sum() / weight if weight else 0.0
    return {
        "coin_count": int(len(df)),
        "stablecoins_excluded": int(stablecoins_excluded),
        "total_market_cap_usd": float(df["market_cap_usd"].sum()),
        "total_volume_24h_usd": float(df["volume_24h_usd"].fillna(0).sum()),
        "coins_up": int((df["change_24h_pct"] > 0).sum()),
        "coins_down": int((df["change_24h_pct"] < 0).sum()),
        "weighted_change_24h_pct": round(float(weighted), 3),
    }


def run_transform(raw_path: Path, out_dir: Path = config.PROCESSED_DIR) -> dict:
    """Run every transformation and write the four output files. Returns their paths."""
    cleaned = clean(load_raw(raw_path))
    coins = add_derived(filter_stablecoins(cleaned))
    if coins.empty:
        raise ValueError("No coins left after cleaning and filtering.")

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "coins_clean": out_dir / "coins_clean.csv",
        "tier_summary": out_dir / "tier_summary.csv",
        "movers": out_dir / "movers.csv",
        "kpis": out_dir / "kpis.json",
    }
    coins.to_csv(paths["coins_clean"], index=False, encoding="utf-8")
    summarize_by_tier(coins).to_csv(paths["tier_summary"], index=False, encoding="utf-8")
    top_movers(coins).to_csv(paths["movers"], index=False, encoding="utf-8")
    kpis = compute_kpis(coins, stablecoins_excluded=len(cleaned) - len(coins))
    paths["kpis"].write_text(json.dumps(kpis, indent=2), encoding="utf-8")
    return {"paths": {name: str(path) for name, path in paths.items()}, "kpis": kpis}


def transform_market_data(tool_context: ToolContext) -> dict:
    """Clean, filter, enrich and aggregate the raw market data saved by the ingest step.

    Takes no arguments: it reads the raw file path from session state.
    Returns a small status dictionary with row counts and output paths.
    """
    raw_path = tool_context.state.get("raw_path")
    if tool_context.state.get("pipeline_error") or not raw_path:
        return {"status": "error", "error_message": "No raw data: the ingest step did not finish."}
    try:
        result = run_transform(Path(raw_path))
    except (OSError, ValueError, KeyError) as exc:
        tool_context.state["pipeline_error"] = "transform"
        return {"status": "error", "error_message": f"Transform failed: {exc}"}

    tool_context.state["processed_paths"] = result["paths"]
    kpis = result["kpis"]
    return {
        "status": "success",
        "coins_kept": kpis["coin_count"],
        "stablecoins_excluded": kpis["stablecoins_excluded"],
        "files": [config.relative(p) for p in result["paths"].values()],
    }
