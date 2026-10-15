---
slug: importing-existing-test-cases
title: Importing existing test cases
category: Requirements and test cases
summary: Bringing a spreadsheet of manual tests into NextGen2
sort_order: 40
---

If you already have manual test cases in Excel or CSV, you can bring them in
rather than rewriting them.

## How it works

Upload the file. NextGen2 reads it, works out which column is the title, which
is the steps, which is the expected result, and shows you what it found.

Nothing is saved at this point. The preview exists so you can correct the
column mapping before anything is written.

## Why there is a preview step

Spreadsheets of test cases are rarely tidy. Headers sit on the third row,
columns are merged, one file uses "Expected Result" and the next uses
"Expected outcome". A column detected wrongly and imported silently would
corrupt hundreds of test cases at once, and unpicking that is far more work
than confirming a mapping.

## After import

Imported test cases behave like any other: they can be kept manual, marked for
automation, added to suites, and reported on.
