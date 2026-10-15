---
slug: requirement-test-case-steps
title: Requirements, test cases and steps
category: Requirements and test cases
summary: The three levels, and why the structure matters
sort_order: 30
---

NextGen2 organises everything in three levels:

- **Requirement** â€” what the system must do. Comes from a document or a Jira
  issue.
- **Test case** â€” one thing being verified about that requirement.
- **Step** â€” one action or check inside a test case.

One requirement usually produces several test cases. One test case always has
several steps.

## Why this structure is worth insisting on

A flat list of test cases cannot answer the question an auditor actually asks:
*which requirement does this test verify, and is every requirement verified by
something?*

Because each test case carries the requirement it came from, NextGen2 can
produce a traceability matrix directly, rather than reconstructing it by hand
afterwards.

## What this means in practice

- A requirement with no test cases shows up as a gap.
- A test case with no requirement is a warning sign, not a convenience.
- When a requirement changes, NextGen2 knows precisely which test cases are
  affected. See *Understanding sync status flags*.
