# Kavach backend — COMPLETE, WORKING. Nothing to copy or merge.

This is your full real backend with every fix baked in. DELETE your old
C:\Varsha\Baxter\Kavach\backend and put THIS folder there instead.
Do NOT copy files from any other zip into it — that mixing is what kept
breaking things. This folder is complete on its own.

## Baked-in fixes
- .env is now read automatically (load_dotenv) -> no more "LLM_API_URL not set"
- provider_type config switch (gemini / azure_openai / simulation)
- simulation provider that generates real canned scenarios with NO key
- azure provider (for Baxter devs later)
- CORS set to localhost:9000 (matches your frontend)
- .env pre-filled for local simulation

Verified: the full app imports and reads .env with an empty environment.

## Run it (3 commands)

    cd backend
    pip install -r requirements.txt
    pip install python-multipart python-dotenv
    uvicorn api.main:app --reload --port 9090

Open http://127.0.0.1:9090/docs -> you'll see ALL routes:
applications, test-suites, proposals, executions, reports.

## The .env is already filled for you

It's set to SIMULATION mode (no LLM key needed). Only thing to check:
the DATABASE_URL password. It's set to:
    postgresql://postgres:baxter123@127.0.0.1:5432/nextgen2
If your postgres password isn't baxter123, edit that one line in .env.

## First-time DB setup (if not done yet)

    psql -U postgres -d nextgen2 -f db/schema.sql
    psql -U postgres -d nextgen2 -f seed_dummy_data.sql   (if you have it)

## Generate scenarios — how it works now

In the UI: New Test Suite -> enter a name + a URL (e.g. http://localhost:3000)
-> Generate scenarios. In simulation mode it produces 8 realistic
scenarios with no LLM key.

NOTE: survey opens a real headless Chrome to look at the URL, so Chrome
must be installed. If Generate fails, watch THIS terminal — it prints
the real reason.
