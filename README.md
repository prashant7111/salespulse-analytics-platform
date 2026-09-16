# SalesPulse

**SalesPulse — Sales Intelligence & Performance Analytics Platform**

A schema-driven Flask analytics workspace that lets a user upload a dataset, inspect its quality, discover usable measures/dimensions, and explore KPI and chart views without hard-coding a single dataset schema.

## Highlights

- CSV, TSV, Excel, JSON, JSONL and Parquet ingestion
- Automatic schema profiling: rows, columns, missing values, duplicates, dates and numeric measures
- KPI, trend, categorical and distribution views generated only when the source supports them
- Data Explorer with a clean table view and CSV export
- Built-in Superstore-style example dataset for a quick first run
- User registration/login with Werkzeug password hashing and HTTP-only sessions
- Per-user in-memory analytical workspace
- Replayable live demo stream
- Optional live JSON API ingestion and webhook ingestion
- Responsive multi-page interface with a restrained white professional design
- Health endpoint, Docker support, WSGI entry point and GitHub Actions CI

## Tech stack

**Python · Flask · Pandas · SQLite · HTML/CSS/JavaScript · ECharts**

## Local run

### Option A — VS Code
1. Open `app.py`.
2. Press **Run Python File**.
3. The launcher installs missing packages from `requirements.txt` and opens SalesPulse on port 5050.

### Option B — terminal

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

The local demo account is created automatically on first run:

- Email: `demo@salespulse.local`
- Password: `salespulse`

## Deployment

Hosted deployments should use `wsgi.py` with Gunicorn rather than the local launcher. The repository includes a `Dockerfile`, `Procfile` and `render.yaml`.

Set a strong `SALESPULSE_SECRET_KEY` in the host's secret/environment settings. Do not commit `.env`, database files, session secrets or API tokens.

### Docker

```bash
docker build -t salespulse .
docker run --rm -p 8000:8000 -e SALESPULSE_SECRET_KEY=change-me salespulse
```

### Render

The included `render.yaml` can be used as a starting point for a Render web service. Review storage and environment settings before using it for real user data.

## Data and persistence

The included `data/train.csv` is the built-in example. Runtime SQLite state and uploaded files are intentionally ignored by Git. The current app keeps active datasets in memory, so uploaded data is not durable across restarts and should not be treated as production-grade storage.

For a multi-instance production system, replace local SQLite/file storage with managed PostgreSQL/object storage and move ingestion jobs to a queue/worker architecture.

## Security notes

- Passwords are stored as Werkzeug password hashes, not plaintext.
- Session cookies are HTTP-only and SameSite=Lax.
- Hosted deployments can enable Secure cookies with `SALESPULSE_COOKIE_SECURE=1`.
- Live API URL validation blocks local/private/reserved network targets to reduce SSRF risk.
- Set `SALESPULSE_WEBHOOK_TOKEN` to require the `X-SalesPulse-Token` header on webhook ingestion.
- Never place API bearer tokens or production secrets in source control.

This is a portfolio/development application, not a complete security-reviewed production SaaS.

## Architecture

```text
CSV / Excel / JSON / Parquet / API / Webhook
                    │
                    ▼
              Pandas ingestion
                    │
          schema + quality profiling
                    │
                    ▼
             analytics functions
                    │
                    ▼
               Flask REST API
                    │
                    ▼
            HTML/CSS/JS + ECharts
```

## Project structure

```text
salespulse-v4/
├── app.py                 # one-click local launcher
├── server.py              # Flask app + analytics/API routes
├── wsgi.py                # hosted WSGI entry point
├── requirements.txt
├── Dockerfile
├── Procfile
├── render.yaml
├── .env.example
├── data/
│   └── train.csv
├── static/
├── templates/
├── .github/workflows/ci.yml
├── PROJECT_GUIDE.md
└── README.md
```

## Interview talking points

**How does SalesPulse handle different datasets?**
It profiles the actual schema first. Numeric measures, dates and categorical dimensions are detected from the incoming columns, then the frontend renders views that those fields can support.

**What happens when a field is missing?**
The application does not invent values. A dependent view is omitted or explained as unavailable.

**Is the live demo external real-time data?**
No. The live demo replays the included historical dataset in small batches. External live ingestion requires an authorized JSON API or webhook.

**What would you change for a larger production system?**
Managed PostgreSQL/object storage, background workers, stronger RBAC/CSRF/rate limiting, durable job state, centralized logging/monitoring, automated integration tests and a reviewed deployment/security model.

## License

No license is currently granted for redistribution of the included dataset or project. Add an appropriate license only after confirming the rights to every included asset and dataset.
## Windows quick start

Double-click `Run SalesPulse.bat`, or run `python app.py` from the project folder. The local launcher installs missing Python dependencies from `requirements.txt` and starts the Flask server on port 5050.
