---
slug: creating-an-application
title: Setting up an application
category: Getting started
summary: The first thing to create, and what hangs off it
sort_order: 20
---

An **application** is the system under test. Everything else hangs off one:
requirements, test cases, scripts, environments, suites, schedules and
reports.

## Creating one

1. Open **Applications** in the left navigation.
2. Add an application, giving it a name and a base URL.
3. Click into it to reach its detail screen, where everything else lives:
   scenarios, test cases, environments, suites, runs and test data.

## The base URL is a starting point, not the answer

The URL here is a convenience. Once you add environments, the URL used at run
time comes from the **environment**, not from the application and not from
the test case.

That is deliberate: a test case is written once and run against dev, test and
stage. If the URL lived in the test, you would need three copies of it.

## What to do next

- Add at least one environment, so runs have somewhere to point.
- Upload a requirements document or connect a Jira project, so there is
  something to generate test cases from.
- Use **Generate scenarios**, then **Generate scripts** for the test cases
  you decide to automate.

## Removing one

**Archive** hides an application without losing anything. **Delete
permanently** is separate, and means it. Archive unless you are certain.
