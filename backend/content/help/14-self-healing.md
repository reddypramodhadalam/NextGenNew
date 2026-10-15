---
slug: self-healing
title: Self-healing failed scripts
category: Results
summary: Diagnosing a failure and approving or rejecting the proposed fix
sort_order: 20
---

When a script fails, NextGen2 can work out why and propose a fix.

## The word "self-healing" oversells it

The name is the industry's, not a description of what happens. NextGen2
diagnoses and **proposes**. It does not apply anything on its own. A human
approves or rejects every fix, and that decision is recorded.

An automation tool that silently rewrote its own tests until they passed would
be worse than useless â€” it would report green while verifying nothing.

## Using it

1. Open the failed execution and run a diagnosis.
2. Read what NextGen2 thinks went wrong and what it suggests changing.
3. **Approve** to apply the fix, or **reject** to record that you looked and
   disagreed.

## Rejections are worth recording

A rejection is not a wasted action. It is evidence that a person reviewed the
failure and made a decision, which is exactly what an auditor asks for. It also
keeps the healing history honest about how often the suggestions are right.
