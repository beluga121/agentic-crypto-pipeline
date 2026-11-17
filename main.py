"""Run the agentic crypto data pipeline.

Usage:
    python main.py               # upload to the private GCS bucket in GCS_BUCKET
    python main.py --local-only  # write to output/gcs_mirror/ instead of GCS
"""

import argparse
import asyncio
import logging
import os
import sys
import uuid

APP_NAME = "crypto_pipeline"
USER_ID = "local_user"
KICKOFF = "Run the crypto data pipeline now."

# Session-state keys that each step must fill in. If any is missing at the
# end, the run did not complete, even if no exception was raised.
REQUIRED_STATE = {
    "raw_path": "ingest",
    "processed_paths": "transform",
    "gcs_uris": "store",
    "report_path": "report",
    "report_uri": "report upload",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agentic CoinGecko -> GCS data pipeline (Google ADK).")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Skip Google Cloud Storage and write the bucket layout to output/gcs_mirror/.",
    )
    return parser.parse_args()


def describe_llm_error(exc: Exception) -> str:
    """Turn a Gemini API error into a short, actionable message."""
    code = getattr(exc, "code", None)
    text = str(getattr(exc, "message", "") or exc)
    if code == 404 or "not found" in text.lower():
        return "Gemini model not found. Set GEMINI_MODEL in .env to a current model (see README)."
    if code in (400, 401, 403) and "key" in text.lower():
        return "Gemini rejected the API key. Check GOOGLE_API_KEY in .env."
    if code == 429:
        return "Gemini quota or rate limit reached. Wait a minute and try again."
    if code in (503, 504):
        return (
            "Gemini is overloaded right now (it kept retrying). Try again in a few minutes, "
            "or set GEMINI_MODEL=gemini-3.5-flash-lite in .env."
        )
    return f"Gemini call failed: {text}"


async def run_pipeline() -> int:
    """Run the agents once and return a process exit code (0 = success)."""
    # Imported here so configuration errors are reported before ADK loads.
    from google.genai import errors as genai_errors
    from google.genai import types
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    from pipeline import config
    from pipeline.agent import root_agent

    session_service = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)
    session_id = f"run-{uuid.uuid4().hex[:8]}"
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    kickoff = types.Content(role="user", parts=[types.Part(text=KICKOFF)])

    step = 0
    current_author = None
    try:
        async for event in runner.run_async(user_id=USER_ID, session_id=session_id, new_message=kickoff):
            if event.author != current_author and event.author != "user":
                current_author = event.author
                step += 1
                print(f"\n[{step}/4] {current_author}")
            for call in event.get_function_calls():
                print(f"      -> calling tool {call.name}()")
            if event.is_final_response() and event.content and event.content.parts:
                text = "".join(part.text or "" for part in event.content.parts).strip()
                if text:
                    print(f"      {config.mask_secrets(text)}")
    except (genai_errors.ClientError, genai_errors.ServerError) as exc:
        print(f"\nERROR: {config.mask_secrets(describe_llm_error(exc))}", file=sys.stderr)
        return 1

    # Trust session state, not the LLM's words: check every step's output.
    session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    state = session.state if session else {}
    missing = [step_name for key, step_name in REQUIRED_STATE.items() if not state.get(key)]
    if missing:
        failed = state.get("pipeline_error", missing[0])
        print(f"\nERROR: the pipeline did not finish. First failing step: {failed}.", file=sys.stderr)
        print("See the agent messages above for the reason.", file=sys.stderr)
        return 1

    print("\nPipeline finished.")
    print(f"  Stored files:  {len(state['gcs_uris']) + 1} ({config.storage_mode()} mode)")
    print(f"  Report stored: {state['report_uri']}")
    print(f"  Open locally:  {state['report_path']}")
    return 0


def main() -> int:
    args = parse_args()
    if args.local_only:
        os.environ["STORAGE_MODE"] = "local"

    from pipeline import config

    errors = config.validate(local_only=config.storage_mode() == "local")
    if errors:
        print("Configuration problem:", file=sys.stderr)
        for message in errors:
            print(f"  - {message}", file=sys.stderr)
        print("Copy .env.example to .env and fill in the values (see README).", file=sys.stderr)
        return 2

    # Keep the console readable: library warnings are not needed for a normal run.
    logging.basicConfig(level=logging.ERROR)

    print(f"Crypto data pipeline | model: {config.gemini_model()} | storage: {config.storage_mode()}")
    try:
        return asyncio.run(run_pipeline())
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
