---
slug: reading-executions
title: Reading execution results
category: Results
summary: What a run recorded, and how to see why something failed
sort_order: 10
---

The Executions screen shows what ran, when, against which environment, and what
happened.

## Reading a failure

Open the execution to see the step that failed, the error, and any screenshot
taken at the point of failure. The screenshot is usually the fastest route to
the answer: it shows the page as the browser actually saw it, which is often
not the page you assumed.

## Full history is kept

Every execution of a script is retained, not just the most recent. That is
deliberate. "It passed last Tuesday and fails now" is only answerable if last
Tuesday is still on record, and in a regulated environment the history is
itself the evidence.

## A failure is not always a defect

Three different things look identical at first glance:

- The application is genuinely broken.
- The application changed legitimately and the test is now out of date.
- The test was fragile and the application is fine.

Working out which is a judgement call. NextGen2 can suggest a cause and propose a
fix â€” see *Self-healing* â€” but it does not decide.
