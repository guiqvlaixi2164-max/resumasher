---
description: Tailor resume + cover letter markdown for a specific job posting
---

Run the **resumasher** skill against `$ARGUMENTS` as the job source (a file path, a URL, or literal pasted JD text).

Execute the full five-phase workflow end-to-end without asking for confirmation between phases. The student's `resume.md` (or `resume.pdf`) and any existing `.resumasher/config.json` are in the current working directory.

The deliverables are markdown files the student formats themselves. Do not render PDFs.

If `$ARGUMENTS` is empty, prompt the student once for the job source via the `question` tool, then proceed.
