---
slug: connecting-jira
title: Pulling requirements from Jira
category: Requirements and test cases
summary: Using a Jira project as the requirements source
sort_order: 50
---

Instead of uploading a document, NextGen2 can read requirements directly from a
Jira project.

## What it reads

Issues of the configured type in the configured project. Each issue's summary
and description become one requirement block, led by its Jira key, so every
test case generated from it traces back to that key.

Descriptions written in Jira keep their structure: paragraphs stay paragraphs
and acceptance-criteria bullets stay bullets.

## Which project

The requirements project is separate from the project support tickets go to.
They must not be the same one â€” a support ticket filed into the requirements
project would be read back as a requirement and generate test cases from
itself.

## The connection is outbound only

NextGen2 calls Jira. Jira never calls NextGen2. There is no inbound endpoint to
open and nothing for a firewall to allow, which is why this works inside a
corporate network where a webhook would not.
