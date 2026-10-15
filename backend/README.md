# NextGen2 — Backend

FastAPI + PostgreSQL backend for the NextGen2 AI test automation platform.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in environment variables
cp .env.example .env
# Edit .env — set DATABASE_URL, LLM_API_URL, LLM_API_KEY, LLM_MODEL

# 3. Create the database (PostgreSQL must already be running)
psql -U postgres -c "CREATE DATABASE nextgen2;"
psql -U postgres -d nextgen2 -f db/schema.sql

# 4. Run the server
uvicorn api.main:app --reload --port 8000
```

## API docs
Visit http://localhost:8000/docs after starting the server.
