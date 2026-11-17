"""Report step: render the processed data as one self-contained HTML page.

Design choices:
- Python computes chart geometry (bar widths and row positions) as plain
  numbers; the Jinja2 template turns them into inline SVG. Because all text,
  including coin names from the API, goes through the template with
  autoescape on, nothing from the API can inject HTML. We never use |safe.
- SVG bars use percentage widths, so charts stretch to any screen width
  while the text inside them stays at a readable, fixed size.
- No JavaScript, web fonts or external files: the page works offline and
  under a strict Content-Security-Policy.
"""

import json
import math
from datetime import datetime
from pathlib import Path

import pandas as pd
from google.adk.tools import ToolContext
from jinja2 import Environment, FileSystemLoader

from pipeline import config
from pipeline.tools import storage

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_NAME = "report.html.j2"
ROW_HEIGHT = 48  # px per bar row: label line + bar, on the 8px grid
MAX_LABEL_CHARS = 24  # long names are shortened so they never collide with values


# ---------- number formatting (used as Jinja filters) ----------

def _is_missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def fmt_usd(value) -> str:
    """Compact dollars: $2.31T, $456.7B, $12.3M, $12,345."""
    if _is_missing(value):
        return "n/a"
    for threshold, suffix, digits in ((1e12, "T", 2), (1e9, "B", 1), (1e6, "M", 1)):
        if abs(value) >= threshold:
            return f"${value / threshold:,.{digits}f}{suffix}"
    return f"${value:,.0f}"


def fmt_price(value) -> str:
    """Coin price with sensible precision for both $64,956 and $0.00001234."""
    if _is_missing(value):
        return "n/a"
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.4g}"


def fmt_pct(value, digits: int = 2) -> str:
    """Signed percentage with a real minus sign, e.g. +1.23% or −4.50%.

    The explicit sign means gains and losses are readable without color.
    """
    if _is_missing(value):
        return "n/a"
    if round(value, digits) == 0:
        return f"{0:.{digits}f}%"
    sign = "+" if value > 0 else "−"
    return f"{sign}{abs(value):.{digits}f}%"


def trend(value) -> str:
    """CSS class name for a change value: up, down or flat."""
    if _is_missing(value) or value == 0:
        return "flat"
    return "up" if value > 0 else "down"


def shorten(text: str, limit: int = MAX_LABEL_CHARS) -> str:
    """Trim long labels with an ellipsis."""
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------- chart geometry ----------

def bar_rows(items: list[dict], max_value: float) -> list[dict]:
    """Give each bar its vertical position and width as a % of the chart width.

    items: dicts with label, detail, value_text and magnitude (>= 0).
    A tiny minimum width keeps very small values visible.
    """
    rows = []
    for index, item in enumerate(items):
        share = item["magnitude"] / max_value * 100 if max_value > 0 else 0
        rows.append({**item, "y": index * ROW_HEIGHT, "width_pct": round(max(share, 0.5), 2)})
    return rows


def tier_chart(tiers: pd.DataFrame) -> dict:
    """Market-cap share by tier. Scaled to 100% so bar length is the true share."""
    items = [
        {
            "label": row.tier,
            "detail": f"{row.coin_count} coin{'s' if row.coin_count != 1 else ''}",
            "value_text": f"{row.market_cap_share_pct:.1f}%",
            "magnitude": float(row.market_cap_share_pct),
        }
        for row in tiers.itertuples()
    ]
    return {"rows": bar_rows(items, 100.0), "height": len(items) * ROW_HEIGHT}


def mover_chart(movers: pd.DataFrame, direction: str, max_abs: float) -> dict:
    """Bars for gainers or losers. Both charts share max_abs so lengths compare."""
    subset = movers[movers["direction"] == direction]
    items = [
        {
            "label": shorten(row.name),
            "detail": str(row.symbol),
            "value_text": fmt_pct(row.change_24h_pct),
            "magnitude": abs(float(row.change_24h_pct)),
        }
        for row in subset.itertuples()
    ]
    description = "; ".join(f"{i['label']} {i['value_text']}" for i in items) or "None in this snapshot."
    return {"rows": bar_rows(items, max_abs), "height": max(len(items), 1) * ROW_HEIGHT, "description": description}


# ---------- page context and rendering ----------

def build_context(tiers: pd.DataFrame, movers: pd.DataFrame, kpis: dict, run_ts: str) -> dict:
    """Assemble everything the template needs. Pure function, easy to test."""
    generated = datetime.strptime(run_ts, "%Y%m%dT%H%M%SZ")
    max_abs = float(movers["change_24h_pct"].abs().max()) if not movers.empty else 0.0
    gainers = mover_chart(movers, "gainer", max_abs)
    losers = mover_chart(movers, "loser", max_abs)

    top_tier = tiers.sort_values("market_cap_share_pct", ascending=False).iloc[0]
    tier_takeaway = (
        f"{top_tier['tier']} caps hold {top_tier['market_cap_share_pct']:.1f}% of the market cap "
        f"with {int(top_tier['coin_count'])} of {kpis['coin_count']} coins."
    )

    volume_share = 100 * kpis["total_volume_24h_usd"] / kpis["total_market_cap_usd"]
    return {
        "generated_at": generated.strftime("%Y-%m-%d %H:%M UTC"),
        "generated_iso": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_ts": run_ts,
        "kpis": kpis,
        "volume_share_pct": volume_share,
        "tiers": tiers.to_dict(orient="records"),
        "tier_chart": tier_chart(tiers),
        "tier_takeaway": tier_takeaway,
        "gainers": gainers,
        "losers": losers,
        "report_prefix": storage.report_object_name(run_ts).rsplit("/", 1)[0] + "/",
        "processed_prefix": storage.processed_object_name(run_ts, ""),
    }


def make_environment() -> Environment:
    """Jinja2 environment with autoescape ON for every template."""
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    env.filters.update(usd=fmt_usd, price=fmt_price, pct=fmt_pct, trend=trend)
    return env


def render_html(context: dict) -> str:
    """Render the report template to an HTML string."""
    return make_environment().get_template(TEMPLATE_NAME).render(**context)


def load_processed(paths: dict) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Read the tier summary, movers and KPI files written by the transform step."""
    tiers = pd.read_csv(paths["tier_summary"], encoding="utf-8")
    movers = pd.read_csv(paths["movers"], encoding="utf-8", dtype={"name": str, "symbol": str})
    kpis = json.loads(Path(paths["kpis"]).read_text(encoding="utf-8"))
    return tiers, movers, kpis


def build_html_report(tool_context: ToolContext) -> dict:
    """Build the HTML report from the processed files, save it, and upload it.

    Takes no arguments: it reads file paths from session state.
    Ordering note: the report is created after the Store step, so this tool
    uploads index.html itself with the same storage helper. That way the
    final page also lands in GCS (or the local mirror).
    """
    state = tool_context.state
    run_ts, paths = state.get("run_ts"), state.get("processed_paths")
    if state.get("pipeline_error") or not (run_ts and paths):
        return {"status": "error", "error_message": "No processed data: an earlier step did not finish."}

    try:
        tiers, movers, kpis = load_processed(paths)
        html = render_html(build_context(tiers, movers, kpis, run_ts))
        config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = config.REPORT_DIR / "index.html"
        report_path.write_text(html, encoding="utf-8")
    except (OSError, ValueError, KeyError) as exc:
        state["pipeline_error"] = "report"
        return {"status": "error", "error_message": f"Report failed: {exc}"}

    state["report_path"] = str(report_path.resolve())
    try:
        [location] = storage.upload_files([(report_path, storage.report_object_name(run_ts))])
    except (RuntimeError, OSError) as exc:
        state["pipeline_error"] = "report upload"
        return {"status": "error", "error_message": config.mask_secrets(exc), "path": config.relative(report_path)}

    state["report_uri"] = location
    return {"status": "success", "path": config.relative(report_path), "stored_at": config.relative(location)}
