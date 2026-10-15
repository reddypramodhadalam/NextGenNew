---
slug: sync-status-flags
title: Understanding sync status flags
category: Requirements and test cases
summary: What happens when a requirement changes after test cases exist
sort_order: 60
---

Requirements change after test cases have been written. NextGen2 checks for that
and tells you which test cases are affected.

## What a sync pass does

It asks Jira which issues have changed since it last looked, then finds the
test cases that reference those issue keys and were written before the change.
Those test cases are flagged **requirement changed**.

New Jira issues that no test case references yet are reported as new
requirements.

## What a flag means, and what it does not

A flag means: *the requirement behind this test case has moved. Someone should
look.*

It does not mean the test case has been changed. NextGen2 does not regenerate,
edit or retire a test case on its own. Flagging is the entire job, and a
person decides what to do next.

That restraint is the point. An automatic rewrite of a validated test case,
triggered by a requirement edit nobody reviewed, is precisely the kind of
unreviewed change to a controlled artefact that a regulated process cannot
allow.

## Clearing a flag

Review the test case against the new wording, update it if it needs updating,
and mark it reviewed. The flag records that a human looked.
