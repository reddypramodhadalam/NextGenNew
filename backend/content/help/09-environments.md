---
slug: environments
title: Environments and URLs
category: Running tests
summary: Running the same test against dev, test and stage
sort_order: 10
---

An environment is a named URL an application can be tested against â€”
typically dev, test and stage.

## Adding one

1. Open **Applications** in the left navigation and click your application.
2. Find the **Environments** panel. With none it says *No environments yet.
   Add one below.*
3. Enter a **Name** â€” the placeholder suggests `dev / test / stage` â€” and the
   URL.
4. **Save**.
5. Click **Make default** on the one most runs should use.

**Edit** changes an existing environment; **Remove** deletes one.

## Why the URL lives here

A test case describes *what* to verify. The environment describes *where*.
Keep them separate and one test case runs everywhere. Put the URL in the test
and you need one copy per environment, and they drift.

At run time the URL is injected from the environment you chose. The script
itself contains no hostname.

Because environments are stored in the database and edited here, whoever owns
the application can change a URL without a deployment.

## Choosing one at run time

Runs and schedules both ask which environment to use. A schedule has an
**Environment** field; an ad-hoc run offers the same list, plus **Custom
URLâ€¦** for a one-off.

## If you have not created any

NextGen2 falls back to the application's base URL so runs have somewhere to
point. That is a convenience for getting started, not a substitute for
setting up environments properly.
