---
slug: what-is-nextgen2
title: What NextGen2 does?
category: Getting started
summary: The shape of the tool, and why it works in separate stages
sort_order: 10
---

NextGen2 turns written requirements into test cases, turns approved test cases
into automation scripts, and runs those scripts on a schedule. It reports what
passed, what failed, and which requirements are covered.

## The stages are separate on purpose

Nothing runs end to end on its own. Each stage produces something a person
reviews before the next stage begins:

1. A requirements document or Jira project is read, and **test cases are
   proposed**. Nothing is generated yet.
2. You review the proposals, edit them, and decide which to keep manual and
   which to automate.
3. **Scripts are generated** for the ones you approved. Nothing is executed
   yet.
4. **Scripts are executed**, on demand or on a schedule.

This is not caution for its own sake. In a regulated environment an
unreviewed change to a validated artefact is the thing that fails an audit,
and one mistake early in a chain propagates through everything downstream.
Separating the stages means an error is caught by a person at the step where
it is cheapest to fix.

## What NextGen2 does not do

- It does not edit a test case on your behalf. When a requirement changes,
  NextGen2 flags the affected test cases and a person decides what to do.
- It does not approve its own work. Generation is assisted; verification is
  human.
- It does not delete history. Superseded test cases, scripts and articles are
  retained and marked, never removed.
