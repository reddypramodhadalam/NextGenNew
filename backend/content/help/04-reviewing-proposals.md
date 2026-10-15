---
slug: reviewing-proposals
title: Reviewing proposed test cases
category: Requirements and test cases
summary: The review gate, and what Keep and Automate mean
sort_order: 20
---

After a survey, NextGen2 shows the test cases it proposes. Nothing has been
generated and nothing has been saved as final. This screen is the gate.

## What to do here

- Read each proposal against the requirement it came from, which is shown
  beside it.
- Edit the wording, the steps, or the expected result.
- Remove proposals that do not make sense.
- Decide **Keep** or **Automate** for each one.

## Keep versus Automate

- **Keep** means the test case stays manual. It is stored, reported on, and
  counts towards coverage â€” it simply will not have a script generated.
- **Automate** means a script will be generated for it when you run
  generation.

Not everything should be automated. A test that runs twice a year, or depends
on a physical device, or needs a human judgement call, is better kept manual
and honestly recorded as manual than automated badly.

## Nothing proceeds without this step

Generation only ever runs against proposals you accepted. If you skip the
review, nothing downstream happens â€” which is the intended behaviour, not a
missing feature.
