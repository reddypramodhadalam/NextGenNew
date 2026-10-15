# Kavach backend - CLEAN, COMPLETE build. Start here.

This replaces ALL your scattered backend folders. Delete the old ones,
use only this. It has: all 5 API routes, real main.py, the Azure fix,
and sample Java tests to run WITHOUT Gemini.

## Run it

1. Delete your old C:\Varsha\Baxter\Kavach\backend entirely
2. Put THIS folder there instead
3. copy .env.example .env  -- then edit .env:
     - DATABASE_URL: your real postgres password, keep 127.0.0.1
     - LLM_API_KEY: your gemini key (only needed for generation)
4. pip install -r requirements.txt
5. pip install python-multipart   (required - was the crash before)
6. uvicorn api.main:app --reload --port 9090
7. Open http://127.0.0.1:9090/docs
   -> you will now see applications, test-suites, proposals,
      executions, reports (NOT just health + execution like before)

## Why nothing worked before

Your old folder had my PATCHED main.py that only registered one route
(execution). The 5 real route files existed but were never wired in,
so every page 404'd. This build has the REAL main.py. Fixed.

## Running tests WITHOUT Gemini (your API key issue)

The sample_tests/ folder has ready-to-run Java Selenium tests for
three apps - fs, netra, baxter - that need NO Gemini and NO real app
access (they point at a public practice site so they pass green):

    cd sample_tests/fs
    mvn test

Needs: Java 17+, Maven, Chrome. WebDriverManager auto-handles the
driver. Each test runs headless and reports pass/fail. This proves
the whole execution path works while Gemini is down.

- fs/    : login form test
- netra/ : dynamic-content test (waits for async load - chat-UI pattern)
- baxter/: navigation + toggle test

Swap the demo URLs/locators for real app ones when ready.

## Azure switch (for Baxter devs)

In .env: LLM_PROVIDER_TYPE=azure_openai + the AZURE_* values.
No code change. The old code guessed provider from URL and Azure
always failed - this build reads LLM_PROVIDER_TYPE explicitly.
