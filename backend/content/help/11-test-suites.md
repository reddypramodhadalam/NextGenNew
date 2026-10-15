---
slug: test-suites
title: Creating a test suite
category: Running tests
summary: Building smoke, regression and custom suites, step by step
sort_order: 30
---

A suite is a named group of test cases you run together. A suite belongs to
one application.

## Creating a suite

1. Open **Applications** in the left navigation.
2. Click the application you want the suite for.
3. Scroll to the suites panel. If you have none it says *No suites yet*.
4. Click one of the three buttons below that panel:
   - **New smoke suite**
   - **New regression suite**
   - **Custom**
5. A picker opens with test cases already selected for you:
   - **Smoke** pre-selects your P0 and P1 automated tests â€” the critical
     path, meant to finish in minutes.
   - **Regression** pre-selects every automated test, because regression
     means "did anything break".
   - **Custom** selects nothing; you choose.
6. Adjust the selection. The pre-selection is a starting point, not a rule.
7. Name it. Smoke and regression suites are named for you â€” *Smoke Suite 1*,
   *Regression Suite 1* â€” and you can change that.
8. Save.

The suite now appears in the panel showing how many test cases it holds, how
many are automated, and how many have no script yet.

## Building one from the Scenarios screen instead

If you are already looking at test cases and want to group the ones in front
of you:

1. Open **Scenarios** in the left navigation.
2. Filter to what you want â€” by application, priority, or automated status.
3. Tick the test cases.
4. Click **Add to suite** in the bar that appears.

All the selected test cases must belong to one application, because a suite
does. If they do not, the button explains that rather than guessing which
application you meant.

## Running a suite

From the suites panel on the application, click **Run** beside it. The button
reads *Runningâ€¦* while it goes.

## Why suites rather than running everything

Running everything sounds safer and behaves worse. A three-hour suite gets
run once a week, so feedback arrives a week after the change that caused the
failure. A ten-minute smoke suite gets run on every build, which is where
most defects are actually caught.

Suites are also what schedules point at, so this is the unit of unattended
running.
