"""Store step: upload raw and processed files to a private GCS bucket.

Object layout (date-partitioned, so tools like BigQuery can prune by dt=):
    raw/coingecko/markets/dt=YYYY-MM-DD/<run_ts>.json
    processed/coingecko/markets/dt=YYYY-MM-DD/<run_ts>/<file>
    reports/dt=YYYY-MM-DD/<run_ts>/index.html

In local mode (--local-only or STORAGE_MODE=local) the same layout is written
under output/gcs_mirror/ instead, so the pipeline runs without a GCP project.

This module never creates buckets and never changes access settings:
the bucket and every object stay private.
"""

import shutil
from pathlib import Path

from google.adk.tools import ToolContext

from pipeline import config

CONTENT_TYPES = {
    ".json": "application/json",
    ".csv": "text/csv; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}

NOT_LOGGED_IN = (
    "Google Cloud credentials are missing or expired. "
    "Run: gcloud auth application-default login"
)


def partition_date(run_ts: str) -> str:
    """Turn a run timestamp like 20261117T153000Z into 2026-11-17."""
    return f"{run_ts[0:4]}-{run_ts[4:6]}-{run_ts[6:8]}"


def raw_object_name(run_ts: str) -> str:
    """GCS object name for the raw snapshot."""
    return f"raw/coingecko/markets/dt={partition_date(run_ts)}/{run_ts}.json"


def processed_object_name(run_ts: str, filename: str) -> str:
    """GCS object name for one processed file."""
    return f"processed/coingecko/markets/dt={partition_date(run_ts)}/{run_ts}/{filename}"


def report_object_name(run_ts: str) -> str:
    """GCS object name for the HTML report."""
    return f"reports/dt={partition_date(run_ts)}/{run_ts}/index.html"


def content_type_for(path: Path) -> str:
    """Pick the Content-Type header from the file extension."""
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _open_bucket(bucket_name: str):
    """Return a GCS bucket handle after checking it is reachable.

    Raises RuntimeError with a message that names the likely cause.
    Imports live here so local-only runs never touch Google Cloud libraries.
    """
    from google.api_core import exceptions as gexc
    from google.auth.exceptions import GoogleAuthError
    from google.cloud import storage

    try:
        client = storage.Client(project=config.gcp_project())
    except GoogleAuthError:
        raise RuntimeError(NOT_LOGGED_IN) from None

    bucket = client.bucket(bucket_name)
    try:
        bucket.reload()  # cheap metadata read that proves the bucket exists
    except gexc.NotFound:
        raise RuntimeError(
            f"Bucket '{bucket_name}' does not exist. Create it first (see README), "
            "or check GCS_BUCKET and the project."
        ) from None
    except gexc.Forbidden:
        # A 403 here still proves the bucket exists. The least-privilege role
        # (Storage Object Creator) can upload objects but cannot read bucket
        # metadata, so we carry on and let the upload itself decide.
        pass
    except GoogleAuthError:
        raise RuntimeError(NOT_LOGGED_IN) from None
    except gexc.GoogleAPICallError as exc:
        raise RuntimeError(f"Could not reach bucket '{bucket_name}': {exc.message}") from None
    return bucket


def upload_files(files: list[tuple[Path, str]], run_mode: str | None = None) -> list[str]:
    """Upload (local_path, object_name) pairs and return where each one landed.

    Returns gs:// URIs in GCS mode, or local mirror paths in local mode.
    """
    run_mode = run_mode or config.storage_mode()

    if run_mode == "local":
        locations = []
        for path, object_name in files:
            target = config.GCS_MIRROR_DIR / Path(object_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            locations.append(str(target))
        return locations

    from google.api_core import exceptions as gexc
    from google.auth.exceptions import GoogleAuthError

    bucket_name = config.gcs_bucket()
    bucket = _open_bucket(bucket_name)
    uris = []
    for path, object_name in files:
        blob = bucket.blob(object_name)
        try:
            # No ACL / predefined_acl argument: objects inherit the bucket's
            # private, uniform access settings.
            blob.upload_from_filename(str(path), content_type=content_type_for(path))
        except gexc.Forbidden:
            raise RuntimeError(
                f"Permission denied writing to '{bucket_name}'. Grant your account the "
                "Storage Object Creator role on this bucket (see README)."
            ) from None
        except GoogleAuthError:
            raise RuntimeError(NOT_LOGGED_IN) from None
        except gexc.GoogleAPICallError as exc:
            raise RuntimeError(f"Upload of {object_name} failed: {exc.message}") from None
        uris.append(f"gs://{bucket_name}/{object_name}")
    return uris


def upload_to_gcs(tool_context: ToolContext) -> dict:
    """Upload the raw and processed files from this run to Google Cloud Storage.

    Takes no arguments: it reads file paths from session state. In local
    mode it copies them into output/gcs_mirror/ with the same layout.
    """
    state = tool_context.state
    run_ts, raw_path, processed = state.get("run_ts"), state.get("raw_path"), state.get("processed_paths")
    if state.get("pipeline_error") or not (run_ts and raw_path and processed):
        return {"status": "error", "error_message": "Nothing to store: an earlier step did not finish."}

    files = [(Path(raw_path), raw_object_name(run_ts))]
    for local_path in processed.values():
        path = Path(local_path)
        files.append((path, processed_object_name(run_ts, path.name)))

    try:
        locations = upload_files(files)
    except (RuntimeError, OSError) as exc:
        state["pipeline_error"] = "store"
        return {"status": "error", "error_message": config.mask_secrets(exc)}

    state["gcs_uris"] = locations
    # Name the destination explicitly so the agent's summary says where the
    # files really went (a local mirror is not "Google Cloud Storage").
    if config.storage_mode() == "local":
        destination = "local mirror folder output/gcs_mirror/ (GCS skipped)"
    else:
        destination = f"private GCS bucket gs://{config.gcs_bucket()}"
    return {"status": "success", "destination": destination, "files_stored": len(locations)}
