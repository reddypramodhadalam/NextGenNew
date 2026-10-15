---
slug: generating-scripts
title: Generating automation scripts
category: Running tests
summary: Turning approved test cases into runnable scripts
sort_order: 20
---

Generation takes the test cases you marked **Automate** and writes automation
scripts for them.

## What generation does and does not do

It writes scripts. It does **not** run them. Execution is a separate,
deliberate action, so generating a hundred scripts never starts a hundred
browsers by surprise.

## Partial results are returned, not hidden

If generation cannot finish â€” a model quota runs out, a service is
unavailable â€” NextGen2 returns what it completed and marks the rest as
incomplete. It does not hang waiting, and it does not discard finished work.

You can re-run generation for the remaining test cases afterwards.

## Reviewing generated scripts

A generated script is a draft by the same logic as everything else here. Read
it before you trust a green result from it. A script that passes for the wrong
reason is worse than one that fails honestly.
