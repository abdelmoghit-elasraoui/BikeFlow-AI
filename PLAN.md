# BikeFlow AI implementation plan

Prepared 6 October 2026. This plan preserves the submitted project commitments. No implementation has started. Session deadlines follow the teacher's canvas; calendar dates and effort estimates should be adjusted once the course schedule is known.

## Intended result

A dispatcher opens a public dashboard, sees current bike and dock availability, receives 30-minute and 60-minute forecasts, and uses a ranked queue with suggested bikes to add or remove. Predictions are saved before their outcomes. Subsequent observations support evaluation, monitoring, candidate retraining, and controlled model promotion.

Required work includes 12–24 months of historical trips, live GBFS archiving, calendar/weather features, historical baselines, locally trained models, FastAPI, the dashboard, robustness/security, Docker, CI/CD, public hosting, and a demonstrated MLOps loop. The direct shortage-risk classifier remains conditional on sufficient archived history, as stated in the submitted proposal.

## Working approach

- Build a small vertical slice first, then expand to the submitted data coverage, both horizons, and eligible stations. A temporary station subset is a development aid, not a replacement for the final scope.
- Validate data and model performance before investing in interface polish.
- Keep a weekly record of decisions, experiments, limitations, and screenshots for the final report.
- Establish basic containerization and checks early; complete industrialization in Sprint 4.
- Ask the teacher about the accepted from-scratch project exception early, without delaying read-only data investigation. This is a course eligibility question, separate from technical feasibility.

## Teacher core requirements (mandatory constraints)

All implementations must run smoothly on standard laptops. These override any
local-only shortcut taken during early sessions:

- Local AI focus: the core AI runs locally (lightweight tabular ML for
  BikeFlow: pooled LightGBM arrival/departure models plus the
  historical-average baseline). Paid, black-box third-party APIs are not
  permitted for the main logic. Models must be evaluated on held-out test
  data and exported as standalone artifacts (versioned model files with
  dataset, parameters, and validation scores, not just notebook output).
- Baseline deployment: reproducible containerization (Docker for API,
  worker, database, and training/prediction jobs) and an automated CI/CD
  pipeline (GitHub Actions) deploying to an accessible environment. The
  workflows removed from GitHub during early local-only sessions must be
  restored, at the latest, as part of the Sprint 4 industrialization
  milestone — continuous collection and deployment cannot stay
  laptop-dependent at final evaluation.
- Standard application security: token-based authentication (JWT with
  operator/admin roles), input validation on every endpoint, API rate
  limits, and zero hardcoded secrets (environment-provided, never in Git).
- Keep setups lightweight: prefer working, measurable implementations over
  heavy local architectures. No Kubernetes, Kafka, or GPU-dependent models
  unless measured results justify them.

## First working session

Complete these in order when implementation is authorized:

1. Inspect the repository URL in the submitted proposal. Determine whether it exists and whether it already contains work before initializing or altering anything. Inspect local instructions and preserve existing changes.
2. Establish the Python environment, dependency configuration, project layout, and example environment configuration. Keep raw data and secrets out of Git.
3. Build a GBFS discovery client and a snapshot collector for station information and station status. Preserve raw payloads, retrieval time, feed time, and station reporting time. Poll according to feed freshness/TTL, initially around once per minute where appropriate. Retry failures without fabricating observations.
4. Run a short collection check. Confirm repeated snapshots, valid counts, timestamps, and readable storage. Distinguish repeated source observations from newly reported states.
5. Inspect a recent monthly trip archive, including all CSV parts. Validate columns, missing station identifiers, timestamps, and station coverage. Build a small diagnostic station-time table.
6. Investigate the mapping between trip station identifiers and GBFS identifiers. Test whether GBFS short_name provides the mapping; verify using names and coordinates. Record unmatched, ambiguous, renamed, and inactive stations.
7. Leave the collector running only in a suitable persistent environment. A sleeping laptop is insufficient for continuous collection. Record collection coverage and outages.

First-session completion evidence: a working collector, a documented sample trip schema, a tested initial station mapping, and a list of unresolved data issues. Do not attempt to finish model training and the dashboard in this session.

## Second working session

First chunk of Sprint 1 data work. No model training beyond a historical-average
baseline. Complete in order:

1. Expanded trip history: download 3 months of trip ZIPs first, extend toward
   the 12-month slice as disk and speed allow. Record filename, SHA-256, row
   counts, and schema per file. Rescan each file with the existing audit
   command; stop on any schema surprise before building targets.
2. Normalized identifiers and timestamps: reuse the verified mapping (2,130
   matched); flag the 71 unmatched and 9k missing IDs without guessing.
   Store instants in UTC, derive weekday/hour and DST flags in
   America/New_York, and verify conversion across one DST boundary.
3. Parquet targets: write partitioned Parquet (`year=/month=/`), then
   aggregate departures from `started_at` and arrivals from `ended_at` into
   30-minute and 60-minute windows with explicit boundaries. Mark genuine
   zero-trip windows apart from closed/missing data with an explicit
   eligibility flag; never silent-zero-fill.
4. Baseline v0: historical average by station, weekday, and time slot for
   both horizons. Report MAE on a held-out final 2 weeks on the same rows
   the later models will use.

Second-session completion evidence: checksums plus row counts for added
months, one station window table with an explicit missing-data flag, and a
baseline MAE for both horizons. Weather join, PostgreSQL, and LightGBM stay
in Sessions 3–4.

## Sprint 1 business logic and AI

Teacher milestone: Session 6.

### Data foundation

- Download an initial 12-month slice of trip data; extend toward 24 months where useful and practical. Track source files, checksums, schema versions, and ingestion results.
- Normalize station identifiers and timestamps. Store instants in UTC and derive time/calendar features in America/New_York, including daylight-saving transitions.
- Store bulk trips and derived training tables as partitioned Parquet. Use PostgreSQL for station metadata, operational snapshots, predictions, and evaluation records, with an explicit retention strategy.
- Aggregate departures by start station/time and arrivals by end station/time. Build both 30-minute and 60-minute targets with explicit window boundaries.
- Distinguish genuine zero-trip windows from unavailable data or stations that were not operating. Document the eligibility rules.
- Join station, calendar, and weather features. Cache weather requests. For retrospective forecast features, choose archived runs available at prediction time; do not leak later observations into earlier forecasts.
- Separate long-history trip features from live-history features. Train a historical model using features available at inference. Add live state/history inputs in a later model once matching archived training examples exist.

### Models and API

- Implement a historical-average baseline by station, day of week, and time slot. Add a simple Poisson/regression baseline if it helps establish performance.
- Train pooled LightGBM arrival and departure models for both horizons. Use shared station features rather than requiring an independent model per station. Compare alternatives only if results justify it.
- Use chronological training, validation, and an untouched final test period. Account for overlapping forecast windows at split boundaries. Fit preprocessing only on training data.
- Measure arrival/departure MAE, performance at busy stations, and performance by horizon. Compare against the historical baseline on the same rows.
- Combine forecasts with live availability to estimate shortage/surplus. Use reported available docks and disabled assets; do not assume every capacity slot is usable.
- Define a transparent operating range and recommendation rule. Label the initial score as a warning/priority score rather than an uncalibrated probability.
- Implement endpoints for current stations, forecasts, ranked recommendations, station details, health, and data freshness.
- Log each prediction with station, issue time, horizon, target interval, model version, input snapshot reference, expected flows, estimated state, and recommendation.

Completion evidence: reproducible ingestion and training, baseline/model comparison for both horizons, working API responses, and predictions persisted before their target interval.

## Sprint 2 integration and frontend

Teacher milestone: Session 7.

- Build a React dashboard with a station map and normal/warning/critical states.
- Show current available bikes/docks, 30-minute and 60-minute forecasts, and ranked recommendations with suggested quantities.
- Provide station details with recent archived availability, predicted flows, forecast assumptions, and data freshness. Show separate arrivals/departures history only where trip-derived data is actually available.
- Refresh from the API. Display loading, stale data, unavailable data, and fallback-model states clearly.
- Include model version and prediction timestamp in useful detail views.
- Add recorded replay using archived observations and predictions so the demonstration remains reliable during feed outages. Label replay explicitly.

Completion evidence: a dispatcher can inspect a station, compare both horizons, understand a recommendation, and identify stale data without uploading files manually.

## Sprint 3 robustness security and evaluation

Teacher milestone: Session 9, including peer-review fixes.

- Validate feed schemas, counts, timestamps, station flags, and weather payloads. Exclude closed/unavailable stations from actionable recommendations.
- Handle retries, rate limiting, missing observations, duplicate ingestion, capacity changes, station additions/removals, and inference failures.
- Provide the historical baseline when the main model cannot run with valid inputs. Mark unavailable current state explicitly rather than using a forecast to disguise missing live data.
- Add JWT authentication and operator/admin roles for protected routes, secure password hashing if passwords are used, secret management, input validation, and API rate limits.
- Evaluate station inventory estimates against later GBFS snapshots. Define snapshot matching tolerance and mark missing outcomes as unevaluable.
- Define empty/full events as occurring anywhere within the forecast horizon. Evaluate with intermediate snapshots, document sampling limitations, and distinguish stations already failed at issue time from newly failing stations.
- Report warning precision, recall, lead time, and top-K capture of future failures. Compare with ranking by current availability alone.
- Evaluate arrival/departure errors against subsequently released trip records, not against net station inventory changes.
- Add meaningful checks for temporal leakage, station mapping, window boundaries/DST, duplicate ingestion, stale feeds, and quantity bounds.
- Document that staff rebalancing and demand constrained by empty/full stations affect observed outcomes. Public data does not establish the causal benefit of dispatch recommendations.

Completion evidence: peer-review issues resolved, tested failure handling, protected routes, and a report separating flow accuracy from station-risk performance.

## Sprint 4 cloud and industrialization

Teacher milestones: Sessions 10–11.

- Finalize Docker packaging for frontend, FastAPI, PostgreSQL, Airflow, and MLflow. Use persistent volumes and private access for internal services.
- Configure GitHub Actions for relevant tests, builds, and deployment with protected secrets.
- Deploy to one suitable persistent cloud environment. Choose the provider after checking current resources and budget. Ensure collection continues independently of the development laptop.
- Store raw snapshots and model artifacts in object storage. Configure retention, backup, health checks, and recovery instructions.
- Schedule collection, feature generation, prediction, outcome matching, evaluation, and retraining through appropriate jobs. Airflow need not orchestrate each individual minute-level HTTP request.
- Track experiments, datasets, parameters, and model versions in MLflow. Use Evidently or explicit statistical checks for data drift and quality monitoring.
- Trigger candidate training when fresh labeled data exists and monitoring warrants it. Drift alone does not justify promoting a replacement.
- Define a promotion gate before testing candidates: improvement on a recent chronological validation set, no unacceptable regression by horizon/busy stations, valid outputs, and operational checks. Preserve an untouched final test set for final reporting.
- Demonstrate both candidate acceptance and rejection, and rollback to a previous version.

Completion evidence: public dashboard, functioning scheduled ingestion/evaluation, reproducible CI/CD, persistent data, and a recorded MLOps promotion/rejection demonstration.

## Conditional classifier

Once archived station history includes sufficient coverage and enough empty/full events, assess a direct shortage-risk classifier. Use chronological evaluation, class-imbalance handling, and probability calibration where probabilities are displayed. If evidence is insufficient, report the collection coverage and keep the forecast-based risk approach. This follows the submitted proposal's condition; do not invent labels or claim validated probabilities.

## Acceptance and decision rules

| Area | Evidence required |
| --- | --- |
| Data | Documented coverage, tested station mapping, quality summaries, and collection uptime |
| Flow forecasting | Both horizons compared with the historical baseline on future dates; disclose results even if the baseline wins |
| Warning quality | Clearly defined labels, evaluable sample size/event counts, and comparison with current-state ranking |
| Dashboard | Complete dispatcher workflow and clear treatment of stale/unavailable data |
| Reliability and security | Required roles, meaningful edge-case checks, and recorded fallback behavior |
| MLOps | Saved predictions/outcomes, versioned candidates, gate results, promotion and rollback |
| Deployment | Public application and continuous collection with persistent storage |

Numerical targets should be agreed after the initial baseline measurement and then fixed before final evaluation. Implementation feasibility does not guarantee that an ML model beats the baseline. A weak result calls for diagnosis and honest reporting, not a fabricated success claim.

## Main dependencies

1. Station mapping precedes reliable trip/live joins.
2. Continuous collection starts immediately and proceeds alongside the other work.
3. Matched training/inference features precede valid model comparisons.
4. Saved predictions and observed outcomes precede credible monitoring.
5. Sufficient labeled history precedes the optional classifier and live-feature retraining.
6. A functioning API precedes dashboard integration; cloud deployment must preserve ingestion continuity.

The largest risks are collection gaps, incompatible station identifiers, temporal leakage, and insufficient observed failure events. Address these early. No truck-route optimization, automatic dispatch, or confirmed operator partnership is required by the submitted proposal.

## Final course deliverables

- Personal repository with setup, architecture, data-source attribution, and reproducible training instructions.
- Public application and recorded backup demonstration.
- Data-quality and chronological model-evaluation report for both horizons.
- Monitoring and MLOps gate evidence, including a rejected candidate and rollback.
- Report/presentation explaining the dispatcher problem, AI contribution, second-domain contribution, measured results, and limitations.

## Sources already verified during planning

- Submitted proposal: `El-Asraoui_Abdelmoghit_BikeFlowAI.pdf`.
- Teacher canvas: `Project Scoping & Innovation Roadmap_Canvas.docx`.
- Teacher clarification: `innovation_vs_baseline.txt`.
- Historical trips and field descriptions: https://citibikenyc.com/system-data
- Monthly archive: https://s3.amazonaws.com/tripdata/index.html
- Live feed discovery: https://gbfs.citibikenyc.com/gbfs/gbfs.json
- Forecast archives: https://open-meteo.com/en/docs/historical-forecast-api

Availability checks on 6 October 2026 found monthly archives for all of 2024 and 2025 and January–September 2026, plus operational station metadata/status feeds. These checks establish source availability; full dataset quality and station matching remain implementation tasks.
