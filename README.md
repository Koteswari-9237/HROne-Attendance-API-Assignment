# Employee Attendance & Analytics API

## Setup

```bash
python -m venv venv
source venv/bin/activate  # On Windows use `venv\Scripts\activate`
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Requires MongoDB 6.0 or newer. Configure connection via `MONGO_URI` and `MONGO_DB` environment variables.

## Overview

This is the implementation of the HROne engineering assignment. It provides an API built with FastAPI and MongoDB for managing employee attendance and calculating analytics.

## Submission details

- All API code is located in `app/main.py`.
- Decisions and index explanations are in `DECISIONS.md`.
- Code review for the starter code is in `REVIEW.md`.

## Features completed

- All endpoints implemented exactly as defined in `openapi.yaml`.
- Business rules R1-R10 fully implemented.
- Database indexes created for performance and constraint guarantees.
- Analyics aggregations run directly on MongoDB.
