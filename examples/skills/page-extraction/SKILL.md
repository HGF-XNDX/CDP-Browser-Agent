---
name: page-extraction
description: Extract named facts from a webpage and report the source URL. Use when a user asks to collect or compare information visible on websites.
---

Open the page requested by the user and identify the requested fields in the observed
text. If a field is missing, mark it as unavailable rather than guessing. Return a
compact table and the URL actually observed. When comparison requires multiple pages,
inspect all requested sources before completing the task.

Read [references/output.md](references/output.md) for the output convention.
