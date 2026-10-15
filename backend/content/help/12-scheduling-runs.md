---
slug: scheduling-runs
title: Scheduling runs
category: Running tests
summary: Running a suite unattended, and stopping it again
sort_order: 40
---

A schedule runs a test suite without anyone starting it. You need a suite
first â€” see *Creating a test suite*.

## Creating a schedule

1. Open **Schedules** in the left navigation.
2. Click **New schedule**.
3. Fill in the form:
   - **Name** â€” what this schedule is for.
   - **Schedule type** â€” leave it on the suite-running type. *Jira sync* is a
     different job that checks for changed requirements rather than running
     tests.
   - **Application** â€” the application the suite belongs to. Choose this
     first; the suite list is empty until you do.
   - **Suite** â€” which suite to run.
   - **Environment** â€” where to run it: dev, test, stage.
   - **Frequency** â€” Once, Hourly, Daily, Weekdays or Weekly.
   - **First run** â€” when it should fire the first time.
   - **Recipients** â€” an address to email when it finishes, such as
     qa-team@baxter.com. Optional.
4. Click **Create schedule**.

## Scheduling a smoke suite

There is nothing special about it. Create the smoke suite on the application,
then create a schedule pointing at it. People usually set a smoke suite to
**Daily** or **Weekdays** early in the morning, so results are waiting when
the team starts.

## Frequency, and why Once is the default

A schedule set to **Once** runs a single time and then stops. That is the
default deliberately: an unwanted single run is a small mistake, an unwanted
recurring job that nobody notices for a month is not.

Change it to Daily, Weekdays or Weekly once you are sure it does what you
expect.

## Running one immediately

**Run now** on the schedule executes it straight away without changing its
cadence. Useful for checking a new schedule does what you meant before
leaving it to fire on its own.

## Stopping a schedule: pause rather than delete

To stop a schedule running â€” nightly, weekly, or at all â€” click **Pause**. Do
not change its frequency and do not delete it.

A paused schedule stays in place with all its settings, stops firing, and
keeps its run history intact. Click **Resume** and it carries on exactly as
it was.

Deleting is rarely what you want: it takes the run history with it. Changing
the frequency does not stop a schedule either â€” it just makes it run at a
different time.
