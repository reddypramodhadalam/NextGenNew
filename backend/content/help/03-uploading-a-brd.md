---
slug: uploading-a-brd
title: Uploading a requirements document
category: Requirements and test cases
summary: How a BRD becomes proposed test cases
sort_order: 10
---

NextGen2 reads a requirements document and proposes test cases from it. Word and
PDF are both supported.

## What gets read

The whole document, including **text held inside tables**. That matters more
than it sounds: requirements are often written as tables, and a reader that
only picks up paragraphs silently misses most of the specification.

Extraction happens once, at upload. The document is not re-read on every
search or run.

## The document is the source of truth

Test cases are proposed from the requirements, not from whatever happens to
exist on the screen being tested.

If a requirement describes something the application does not appear to have,
that is a **coverage finding** and NextGen2 will still propose a test case for
it. It is not dropped quietly. A requirement with no corresponding
functionality is exactly the thing you want surfaced, not hidden.

## After upload

Nothing is generated yet. You get a list of proposed test cases to review. See
*Reviewing proposed test cases*.
