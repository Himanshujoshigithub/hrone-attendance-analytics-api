# Employee Attendance & Analytics API

FastAPI + PyMongo implementation for the HROne Software Engineer Trainee assignment.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (local instance or MongoDB Atlas)

## Run locally

```bash
python -m venv .venv
# Windows PowerShell:
.venv\\Scripts\\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env  # Windows; on macOS/Linux use: cp .env.example .env
uvicorn app.main:app --port 8000
```

Set `MONGO_URI` and `MONGO_DB` in your environment or `.env`. Environment variables already present in the process take precedence over `.env`. The API creates its indexes on startup. `GET /health` pings MongoDB; it returns 200 only when MongoDB is reachable.

Open interactive API docs at http://127.0.0.1:8000/docs.

## Main endpoints
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly?month=YYYY-MM`
- `GET /analytics/departments/summary?month=YYYY-MM`
- `GET /analytics/leaderboard/late?month=YYYY-MM`
- `GET /analytics/departments/{department}/trend?from=YYYY-MM-DD&to=YYYY-MM-DD`
- `GET /admin/explain/{endpoint}`

All instants exposed by the API are epoch milliseconds. MongoDB stores the instants as UTC BSON datetimes. Attendance dates and shift times follow the assignment's IST rules.

## Before submission
1. Run against MongoDB 6.0+ and test each endpoint in `/docs`.
2. Compare the output with the supplied `openapi.yaml` and sample seed data.
3. Inspect the `explain` endpoint for each supported query and confirm index use on your target MongoDB version.
4. Review `REVIEW.md` and `DECISIONS.md` yourself; be prepared to explain the implementation during a live interview.
5. Do not commit `.env`, credentials, virtual environments, or data dumps.

## Known implementation notes
The trend pipeline uses `$densify`, `$fill`, and `$setWindowFields`, so it requires a MongoDB version supporting those aggregation stages (MongoDB 6.0+). The aggregation pipelines use explicit floor-based half-up rounding for non-negative rates and averages because MongoDB `$round` uses ties-to-even. Explain plans can differ by MongoDB version and data distribution; verify them on the evaluation-like dataset.