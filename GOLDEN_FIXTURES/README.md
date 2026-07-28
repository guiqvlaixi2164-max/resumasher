# GOLDEN_FIXTURES

A sample MS Business Analytics student portfolio, used for:
- resumasher end-to-end testing
- Demoing the skill to students before they use their own data
- Regression testing when a future change might affect output quality

## Contents

- `resume.md` — Ana Müller's base resume (intentionally uses non-ASCII characters
  in the name to exercise the Unicode path).
- `sample-jd.md` — a Deloitte Vienna Data Analyst job description, representative
  of what analytics MSc graduates see on the Vienna / DACH market.
- `projects/` — three sample projects (capstone, ML final, text mining) with
  READMEs, notebooks, and Python files.

## Using the fixture

From this directory, run:

```bash
/resumasher sample-jd.md
```

and verify `tailored-resume.md` and `cover-letter.md` land in
`./applications/deloitte-<date>/`.

## Why Ana Müller

Made-up person. The umlaut is deliberate: it exercises the non-ASCII path
through resume reading, folder mining, and prompt substitution. If we ever
ship a version where "Müller" comes back as "M ller", tests catch it before
students do.
