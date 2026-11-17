"""ADK agents: one SequentialAgent that runs four LlmAgents in order.

    crypto_pipeline (SequentialAgent)
      1. ingest_agent    -> fetch_market_data()
      2. transform_agent -> transform_market_data()
      3. store_agent     -> upload_to_gcs()
      4. report_agent    -> build_html_report()

Each agent has exactly one tool. Tools pass file paths to each other through
session state (tool_context.state), so the LLM only ever sees small status
dictionaries, never the market data. Each agent's one-line summary is saved
in state under its output_key.

`root_agent` is the name `adk web` looks for, so `adk web` also works.
"""

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.models import Gemini
from google.genai import types

from pipeline import config
from pipeline.tools.ingest import fetch_market_data
from pipeline.tools.report import build_html_report
from pipeline.tools.storage import upload_to_gcs
from pipeline.tools.transform import transform_market_data

# Temperature 0: we want the same plain status line every run, not creativity.
_GENERATION = types.GenerateContentConfig(temperature=0)

# Gemini sometimes answers "high demand" (503) or "rate limit" (429) for a
# few seconds. Retrying with exponential backoff (waits of about 2, 4, 8 and
# 16 s, capped at 30 s) usually gets through, instead of failing the whole run.
# Not retried: errors that retrying cannot fix, such as a bad key (400/403) or
# an unknown model (404), and 504 timeouts, because each of those has already
# waited about a minute and retrying would make one run take many minutes.
_RETRY = types.HttpRetryOptions(
    attempts=5,
    initial_delay=2.0,
    max_delay=30.0,
    exp_base=2.0,
    http_status_codes=[429, 500, 503],
)

# Shared rules appended to every instruction. Note: ADK fills in {curly}
# placeholders from session state, so the instructions avoid braces.
_RULES = (
    "Call your tool exactly once. It takes no arguments. "
    "If the tool result has status 'success', reply with one short sentence that "
    "restates the numbers and paths from the result. Do not invent numbers. "
    "If the status is 'error', reply with 'ERROR: ' followed by the error_message, "
    "and stop. Do not retry and do not call any other tool."
)


def _step(name: str, description: str, task: str, tool, output_key: str) -> LlmAgent:
    """Build one pipeline step: an LlmAgent with a single tool."""
    return LlmAgent(
        name=name,
        model=Gemini(model=config.gemini_model(), retry_options=_RETRY),
        description=description,
        instruction=f"{task} {_RULES}",
        tools=[tool],
        output_key=output_key,
        generate_content_config=_GENERATION,
    )


ingest_agent = _step(
    "ingest_agent",
    "Downloads the top 100 coins by market cap from CoinGecko.",
    "You are the ingestion step of a crypto data pipeline. Use fetch_market_data "
    "to download the market snapshot and save the raw JSON.",
    fetch_market_data,
    "ingest_summary",
)

transform_agent = _step(
    "transform_agent",
    "Cleans, filters and aggregates the raw market data with pandas.",
    "You are the transformation step of a crypto data pipeline. Use "
    "transform_market_data to clean the raw data, remove stablecoins, and "
    "write the processed files.",
    transform_market_data,
    "transform_summary",
)

store_agent = _step(
    "store_agent",
    "Uploads raw and processed files to the private Google Cloud Storage bucket.",
    "You are the storage step of a crypto data pipeline. Use upload_to_gcs to "
    "store the raw and processed files.",
    upload_to_gcs,
    "store_summary",
)

report_agent = _step(
    "report_agent",
    "Builds the self-contained HTML report and stores it.",
    "You are the reporting step of a crypto data pipeline. Use build_html_report "
    "to create the HTML report and store it.",
    build_html_report,
    "report_summary",
)

root_agent = SequentialAgent(
    name="crypto_pipeline",
    description="Ingest -> Transform -> Store -> Report for CoinGecko market data.",
    sub_agents=[ingest_agent, transform_agent, store_agent, report_agent],
)
