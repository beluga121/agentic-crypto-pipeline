Repository: https://github.com/beluga121/agentic-crypto-pipeline

# Agentic Crypto Data Pipeline: Future Improvements

The pipeline uses four Google ADK agents to ingest the top 100 coins from CoinGecko, transform them with pandas, store raw and processed files in a private Google Cloud Storage bucket, and publish a self-contained HTML report. This document lists concrete next steps that would take it from a working class project to a dependable production service, with the reasoning behind each one.

## Scalability

- **Paginate beyond the top 100.** Loop over `page=1..N` with `per_page=250` and a short pause between requests. Long-tail coins show different behaviour (higher volatility, thinner liquidity), and the tier analysis is more meaningful with more Small-cap coins.
- **Add historical endpoints.** Call `/coins/{id}/market_chart` for the coins being tracked to build daily time series. One snapshot cannot show trends, and history is what the report most lacks.
- **Write Parquet instead of CSV.** Parquet is typed, compressed and columnar, so files are smaller and faster to query, and numbers do not need re-parsing. Keep CSV only as an optional export for spreadsheets.
- **Query the bucket with BigQuery external tables.** The `dt=YYYY-MM-DD` folders are already Hive-style partitions, so an external table over `processed/` can serve SQL and dashboards without moving any data.

## Error handling and resilience

- **Make runs idempotent.** Derive the run key from the API's `last_updated` time instead of the wall clock, and skip the upload if that object already exists (`if_generation_match=0`). Re-running after a partial failure then cannot create duplicates.
- **Add dead-letter storage for bad payloads.** Today a response that fails validation is discarded. Saving it to `deadletter/dt=.../` keeps the evidence for debugging schema changes on CoinGecko's side.
- **Put a circuit breaker on the API.** After repeated 429 or 5xx responses, stop calling CoinGecko for a cool-down period and report "source unavailable" instead of retrying on every scheduled run.
- **Stop the sequence on the first failure.** Later tools already skip work after an error. A custom ADK agent, or the 2.x workflow graph, could end the run immediately and save the remaining LLM calls.

## Monitoring and observability

- **Use structured logging.** Emit one JSON log line per tool call (run key, step, status, row counts, duration). These are easy to filter and turn into metrics.
- **Send logs to Cloud Logging.** When the pipeline runs on Cloud Run, stdout JSON is collected automatically. Log-based metrics can then count failures per step.
- **Turn on ADK tracing.** ADK supports OpenTelemetry traces (for example, exported to Cloud Trace). These show each agent's LLM latency, token use and tool calls on one timeline.
- **Alert on failures and missed runs.** A Cloud Monitoring alert for a non-zero exit code, or for "no new report object in 26 hours", catches silent breakage.

## Scheduling and orchestration

- **Run it on Cloud Scheduler and Cloud Run Jobs.** Package `main.py` in a container, run it as a Cloud Run Job, and trigger it hourly or daily. This is the cheapest option for one job: there is no server to manage, and you pay only for run time.
- **Move to Cloud Composer when there are many pipelines.** Airflow adds dependencies between jobs, backfills and retries per task. That is worth its cost only once several sources feed each other.
- **Deploy the agent to Vertex AI Agent Engine.** Agent Engine hosts ADK agents with managed sessions and scaling, which would let other tools or people trigger the pipeline through an API.

## Testing

- **Add integration tests with recorded responses.** Replay a saved CoinGecko response through the real `requests` code path (for example, with `responses` or VCR-style cassettes). This tests retries and status handling without network calls.
- **Evaluate the agents with ADK eval sets.** Record a reference run, then use `adk eval` to check that each agent calls the right tool exactly once and reports the tool's numbers without inventing any.
- **Set up CI on GitHub Actions.** Run `pytest`, a linter and a secret scanner on every pull request, on Windows, macOS and Linux, to protect the "works on any device" promise.

## Security

- **Keep secrets in Secret Manager.** In the cloud, read `GOOGLE_API_KEY` and `COINGECKO_API_KEY` from Secret Manager instead of `.env`, so keys are never stored on disk and access is audited.
- **Use Workload Identity Federation for CI.** GitHub Actions can authenticate to Google Cloud without any long-lived key file.
- **Run under a dedicated service account.** A dedicated account with only Storage Object Creator on this one bucket limits the damage if the job is compromised.
- **Add VPC Service Controls and dependency scanning.** A VPC-SC perimeter around the storage project blocks data exfiltration. Dependabot and `pip-audit` flag vulnerable pinned packages.

## Cost

- **Add GCS lifecycle rules.** Move `raw/` objects to Nearline after 30 days and Coldline after 90, and delete them after a year. Raw snapshots are rarely read but keep piling up.
- **Use the smallest model that works.** The agents only call one tool and restate a short status, so a Flash-Lite model is likely enough. Use eval sets to confirm the cheaper model behaves the same.
- **Cache and skip unchanged data.** CoinGecko caches `/coins/markets` for about a minute. Skip a run when `last_updated` has not changed since the previous run, to save the API quota, LLM tokens and storage.

## Data quality

- **Add schema contracts with pandera.** Declare column types and ranges (price > 0, market cap ≥ 0, change between −100% and a sane maximum) and fail the transform step with a clear report when they break.
- **Add anomaly checks.** Flag runs where total market cap moves more than X% from the previous run, or where the number of coins drops sharply. These usually mean an API problem rather than a market event.
- **Add freshness checks.** Reject snapshots whose `last_updated` is older than a threshold, so a stale cache is never reported as current data.
- **Deduplicate.** Remove duplicate coin IDs within a run, and deduplicate across runs when building historical tables.

## Visualization

- **Show historical trends.** Once history exists, add sparklines for total market cap and the weighted 24h change, plus a tier-share trend over time.
- **Host behind authentication.** Serve the report from Cloud Run or App Engine behind Identity-Aware Proxy (IAP), so authorised people get a URL while the bucket stays private. This avoids both public buckets and signed URLs.
- **Add interactive filtering carefully.** If filtering is ever needed, add a small, self-hosted script with a matching Content-Security-Policy `script-src` hash, keeping the page offline-capable.

## Prioritized roadmap

**Next month**

- CI on GitHub Actions with tests and secret scanning
- Structured logging and alerting on failures
- GCS lifecycle rules for `raw/`
- Idempotent run keys based on `last_updated`

**Next 3 months**

- Cloud Run Job on Cloud Scheduler, with a dedicated service account and Secret Manager
- Parquet outputs and BigQuery external tables
- Pandera schema contracts and freshness checks
- Integration tests with recorded responses, and ADK eval sets

**Next 6 months**

- Historical endpoints and trend charts
- Report hosted behind IAP
- Pagination beyond the top 100, and a dead-letter store
- Evaluation of Vertex AI Agent Engine and the ADK 2.x workflow runtime
