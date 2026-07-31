---
name: resumasher
description: |
  Tailor the student's resume + write a cover letter for a specific job posting.
  Runs in the student's working directory so it can cite evidence from their
  actual project files (capstone, notebooks, READMEs, PDFs) and their public
  GitHub. Outputs editable markdown in ./applications/<company-slug>-<date>/ —
  the student formats and sends it themselves.
argument-hint: <job-source> [--github <username>]
---

# resumasher

Invoked as `/resumasher <job-source>` from inside the student's resume folder.

`<job-source>` is one of:
- A path to a file containing the job description (`job.md`, `jd.txt`).
- A URL to a job posting.
- Literal text pasted after the command.

Optional flag: `--github <username>` (one-run override of the configured GitHub account).

## What this produces

Two markdown files the student edits and formats themselves:

- `tailored-resume.md` — rewritten, JD-targeted, sections ordered for the market, bullets ranked by relevance.
- `cover-letter.md` — a real letter, ~300 words in 3 paragraphs, citing recent company facts.

Plus `jd.md` (the posting, for the record) and `company-research.md` is *not* written — the research feeds the letter and is discarded.

**resumasher does not render PDFs.** The markdown is the deliverable. Students
format in Word / Google Docs / Pages, which is where a resume gets its final
polish anyway. Do not offer to generate a PDF, and do not install a renderer.

## Prerequisites

Python 3.10+ with `pdfminer.six`, `chardet`, `nbconvert` (see `requirements.txt`).

## Workflow

Five phases. Every deterministic helper is a Python module under `scripts/`;
every LLM phase dispatches a sub-agent.

### Setup: resolve paths in EVERY Bash tool call

⚠️ **The Bash tool runs every command in a fresh shell. Variables set in one Bash tool call do NOT persist to the next.** If you set `SKILL_ROOT` in one call and reference `"$SKILL_ROOT/..."` in the next, it will be empty and the command fails with `permission denied` or `file not found`.

**Every Bash tool call that touches resumasher's code MUST begin with this prologue.** Paste it at the top of every command; don't try to "remember" values from a prior call.

```bash
SKILL_ROOT=""
NEEDS_INSTALL=""
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
for c in \
  "$HOME/.claude/skills/resumasher" \
  "$PWD/.claude/skills/resumasher" \
  "$REPO_ROOT/.claude/skills/resumasher" \
  "$HOME/.codex/skills/resumasher" \
  "$PWD/.codex/skills/resumasher" \
  "$REPO_ROOT/.codex/skills/resumasher" \
  "$HOME/.gemini/skills/resumasher" \
  "$PWD/.gemini/skills/resumasher" \
  "$REPO_ROOT/.gemini/skills/resumasher" \
  "$HOME/.opencode/skills/resumasher" \
  "$PWD/.opencode/skills/resumasher" \
  "$REPO_ROOT/.opencode/skills/resumasher"; do
  [ -n "$c" ] || continue
  [ -f "$c/SKILL.md" ] || continue
  if [ -x "$c/.venv/bin/python" ] || [ -x "$c/.venv/Scripts/python.exe" ]; then
    SKILL_ROOT="$c"; break
  else
    NEEDS_INSTALL="$c"
  fi
done
if [ -z "$SKILL_ROOT" ]; then
  if [ -n "$NEEDS_INSTALL" ]; then
    echo "ERROR: resumasher found at $NEEDS_INSTALL but its Python venv is missing." >&2
    echo "This means install.sh was never run after git clone. Fix:" >&2
    echo "  bash $NEEDS_INSTALL/install.sh" >&2
  else
    echo "ERROR: resumasher is not installed. See https://github.com/earino/resumasher#install" >&2
  fi
  exit 1
fi
RS="$SKILL_ROOT/bin/resumasher-exec"
STUDENT_CWD="$PWD"
```

This sets `SKILL_ROOT` (the installed skill, user- or project-scope), `RS`
(the `bin/resumasher-exec` wrapper that auto-locates the venv Python), and
`STUDENT_CWD` (the student's resume folder, NOT the skill dir).

The prologue distinguishes three cases: SKILL_ROOT set → proceed; NEEDS_INSTALL
set but SKILL_ROOT empty → cloned without running install.sh, message names the
fix; both empty → not installed.

Every helper call looks like:

```bash
"$RS" orchestration <subcommand> [args...]     # discover-resume, mine-context, company-slug, ...
"$RS" github_mine <username>                   # GitHub profile mine
```

Do **not** run `python -m scripts.orchestration` directly — the venv Python has
the dependencies, your system Python doesn't. Use `$RS`.

**Run scratch goes in `$STUDENT_CWD/.resumasher/run/`** — NOT `/tmp/`. That
directory is gitignored, scoped to the student's folder, and wiped at the start
of each run. Create it once, near the top:

```bash
RUN_DIR="$STUDENT_CWD/.resumasher/run"
rm -rf "$RUN_DIR"
mkdir -p "$RUN_DIR"
```

### Interactive prompt pattern (cross-host)

This skill runs on Claude Code, Codex CLI, Gemini CLI, and OpenCode. Each host
has a different tool name but the same contract: present 2+ real options, let
the student type free text in an "Other" field.

- **Claude Code:** `AskUserQuestion`
- **Codex CLI:** `request_user_input` (NOT `ask_user_question` — unshipped)
- **Gemini CLI:** `ask_user`
- **OpenCode:** `question`

Wherever this document says "use the question tool", use whichever your host provides.

⚠️ **All four require a MINIMUM of 2 real options.** "Other" is auto-added and
does NOT count. One option crashes with `InputValidationError: Too small:
expected array to have >=2 items` (Claude) or `"request_user_input requires
non-empty options for every question"` (Codex).

Two mistakes to avoid when collecting a free-text value:

1. Passing only 1 explicit option (API error).
2. A middleman flow where round 1 asks "will you provide a value?" and round 2 collects it (doubles the prompts).

✅ **Pattern A — a default exists** (e.g. you extracted `name` / `email` from `resume.pdf`):

```
Question: "Phone number for the resume?"
  A) Use the value from your resume: "+43 664 1234567"
  B) Skip — don't include phone on the tailored resume
  Other: paste a different phone number
```

✅ **Pattern B — no default exists** (GitHub username — the PDF doesn't contain it):

```
Question: "Do you have a GitHub? We can leverage it for this."
  A) I have one — paste the username/URL in Other below
  B) Skip — leave blank; set github_prompted=true so we don't re-ask
  Other: paste your GitHub username or profile URL
```

Option A exists to satisfy the minimum-2 constraint AND to hint that there IS
an input field; the student answers in Other.

### No interactive tool available — hard-fail fallback

If none of the question tools is available (`codex exec`, a CI script, a host
that ships none), do NOT guess values from context. Silent inference produced
wrong configs in v0.1.

1. Stop before Phase 1.
2. Write a skeleton `.resumasher/config.json` in `$STUDENT_CWD` with every field set to `"__ASK__"`: `name`, `email`, `phone`, `linkedin`, `location`, `github_username`, and `github_prompted: false`.
3. Print exactly this, then exit 2:

   ```
   resumasher needs answers to its setup questions but this host does not
   support interactive prompts. Edit .resumasher/config.json, replace every
   "__ASK__" value with your real answer (use "" to skip optional fields
   like linkedin), then re-run the skill.
   ```

Never infer name, email, or GitHub username from resume content or JD location.

### Sub-agent prompt pattern (cross-host)

Every sub-agent (folder-miner, job-extractor, company-researcher, tailor,
cover-letter) uses a prompt built from runtime content.

**Do NOT build these prompts inline with string interpolation.** Cross-host
testing showed Gemini CLI dispatching a sub-agent with `{resume_text}`
unfilled, producing output that said *"the resume section is a placeholder."*
Use `build-prompt`:

```bash
PROMPT=$("$RS" orchestration build-prompt --kind <kind> --cwd "$STUDENT_CWD" [--out-dir "$OUT_DIR"] [--company "$COMPANY"])
```

`build-prompt` reads the right files from `$RUN_DIR/` / `.resumasher/cache.txt`
/ `$OUT_DIR/`, substitutes them into the kind's template (`scripts/prompts.py`),
and emits the rendered prompt on stdout. If a required file is missing it exits
2 naming the file and the phase that produces it.

**If a prompt is too large to round-trip through a shell variable** (the
`folder-miner` prompt routinely exceeds 100KB on a real GitHub mine; some hosts
cap argv at 128KB), stage it inside `$RUN_DIR/prompts/` — NEVER `/tmp/`:

```bash
mkdir -p "$RUN_DIR/prompts"
"$RS" orchestration build-prompt --kind folder-miner --cwd "$STUDENT_CWD" \
  > "$RUN_DIR/prompts/folder-miner.txt"
PROMPT=$(cat "$RUN_DIR/prompts/folder-miner.txt")
```

`/tmp/` is forbidden for staging: on macOS it's world-readable to other local
users until reboot (exposing resume + JD + project content as plaintext PII),
and files there outlive the run. A Phase 5 cleanup scan sweeps
`/tmp/<kind>-prompt.txt` stragglers as defense-in-depth.

**Pass `$PROMPT` AS-IS — do not paraphrase, summarize, shorten, or rewrite it.**
The compiled prompts are tuned per kind: labeled `<<<...BEGIN>>>/<<<...END>>>`
markers, prompt-injection defenses for UNTRUSTED content, exact ordering of
structural instructions. A weak model that "improves" the prompt ships broken
artifacts that look superficially correct (observed: a Qwen run inverted "Start
with" to "End with" and the cover letter's salutation landed at the bottom).

**The dispatch primitive AND the `subagent_type` value differ per host** — use
the entry matching the CLI you're running in. The wrong value returns
`Unknown agent type: <X>` and burns a dispatch attempt.

- **Claude Code:** `Task` tool with `subagent_type="general-purpose"`.
- **OpenCode:** `task` tool (lowercase) with `subagent_type="general"` (NOT `"general-purpose"`).
- **Gemini CLI:** `@generalist`.
- **Codex CLI:** explicitly instruct the model to spawn a sub-agent — "spawn a sub-agent with the following prompt and return its output." Without that, Codex runs it inline and loses prompt-injection isolation.

The five kinds and their inputs:

| Kind | Reads | Output |
|---|---|---|
| `folder-miner` | `$RUN_DIR/context.txt` | prose summary → save to `.resumasher/cache.txt` |
| `job-extractor` | `$RUN_DIR/jd.txt` | `COMPANY:`, `ROLE:`, `HARD_REQUIREMENTS:`, `PREFERRED:`, `TITLE_VARIANTS:` |
| `company-researcher` | `--company` arg | 3-5 bullet facts with citations |
| `tailor` | resume, cache.txt, jd.txt, keywords, config | tailored resume markdown |
| `cover-letter` | tailored-resume, jd.txt, keywords, research, cache.txt, config | European-format motivation letter, 3-4 paragraphs |

---

### Phase 0 — First-run setup (skip if already done)

```bash
cd "$STUDENT_CWD"
"$RS" orchestration first-run-needed .
```

If it prints `yes` and exits 1, run setup. Print the privacy notice:

> resumasher stores your contact info and application history LOCALLY in
> `.resumasher/` inside this folder. If this folder is a git repo, we will
> add `.resumasher/` to your .gitignore automatically.
>
> Nothing is uploaded. Your resume, job descriptions, and generated files
> never leave this machine. The only network calls resumasher makes are the
> ones you ask for: fetching a job posting URL, reading your public GitHub
> profile, and the company web search.

**Pre-fill from the resume, don't interrogate.** If a resume file is present,
extract its text (`"$RS" orchestration read-resume <path>`) and pull the name,
email, phone, LinkedIn, and location out of its header. Write them straight
into `config.json`. Then show the student what you wrote in ONE question:

```
Question: "I read this contact info off your resume:

    Ana Müller
    ana.mueller@example.com | +43 664 1234567 | linkedin.com/in/anamueller | Vienna, AT

Use it?"
  A) Yes, use it
  B) Something's wrong — I'll edit .resumasher/config.json myself
  Other: paste corrected values
```

That is one round instead of five. Only fall back to per-field Pattern A/B
questions if extraction found nothing usable (no resume, or a scanned PDF).

**GitHub profile:** Pattern B shape ("A) I have one / B) Skip — sets
`github_prompted=true` so we don't re-ask"). Other accepts a username or a
profile URL; strip the prefix.

**Relocation and work authorization** (`relocation_context`). Ask once:

```
Question: "Are you applying to jobs in a country where you're not a
citizen? If so, the cover letter should address it in one or two
sentences — recruiters screen out applications that leave the question
hanging."
  A) No, I'm applying where I already have the right to work
  B) Yes — describe my situation in Other below
  Other: e.g. "Non-EU citizen. Finishing an MS in Vienna, hold a
         post-study work permit, eligible to work in Austria without
         sponsorship. Want to stay for the CEE banking sector."
```

Store the student's answer verbatim in `relocation_context`. Option A
stores `""`.

**Write it verbatim and do not embellish it.** This string is the ONLY
source of facts the cover letter may state about visas, permits, or
eligibility. Never infer a work-authorization status from the student's
nationality, their location, or the JD's country. A cover letter that
misstates someone's right to work is not a style problem — it can cost
them the application and waste the employer's time. If the student's
answer is vague, store the vague version; the letter will be vague, which
is correct.

When `relocation_context` is empty, the cover-letter prompt is explicitly
told to write nothing at all about relocation.

If the student has a `config.json` from before GitHub was a field AND lacks
`github_prompted: true`, ask the GitHub question once and rewrite the config.

Write the config. **The parent `.resumasher/` may not exist yet — `mkdir -p`
first**, otherwise the redirect fails with `zsh: no such file or directory` and
the next phase silently runs against an empty config:

```bash
mkdir -p "$STUDENT_CWD/.resumasher"
cat > "$STUDENT_CWD/.resumasher/config.json" << 'CONFIGEOF'
{
  "name": "...",
  "email": "...",
  "phone": "...",
  "linkedin": "...",
  "location": "...",
  "github_username": "...",
  "github_prompted": true,
  "relocation_context": ""
}
CONFIGEOF
"$RS" orchestration ensure-gitignore .
```

(`ensure-gitignore` is idempotent and exits 0 silently if the folder isn't in a git repo.)

---

### Phase 1 — Intake

**Set up the run scratch directory FIRST** — everything later in this phase writes into it:

```bash
RUN_DIR="$STUDENT_CWD/.resumasher/run"
rm -rf "$RUN_DIR"
mkdir -p "$RUN_DIR"
START_TS=$(date +%s)
echo "$START_TS" > "$RUN_DIR/start-ts.txt"
```

Parse the job source and save the JD to `$RUN_DIR/jd.txt` (later phases read
that path; Phase 3 copies it to `$OUT_DIR/jd.md`):

```bash
JD_MODE=$("$RS" orchestration parse-job-mode "$JOB_SOURCE_ARG")
```

Route the write through `format-jd`. For file and literal modes, pipe
`parse-job-content` directly through it — never round-trip content through a
shell variable, where `echo`-interprets-backslash quirks (zsh, dash, bash with
`xpg_echo`) corrupt the bytes:

```bash
# mode=file or mode=literal — pipe content directly, no shell-string roundtrip:
"$RS" orchestration parse-job-content "$JOB_SOURCE_ARG" \
  | "$RS" orchestration format-jd --mode "$JD_MODE" > "$RUN_DIR/jd.txt"

# mode=url — fetch the page FIRST, then pipe the fetched text with --url set:
echo -n "$FETCHED_PAGE_TEXT" | "$RS" orchestration format-jd --mode url --url "$URL" > "$RUN_DIR/jd.txt"
```

`format-jd` prepends `Source URL: <url>` when `mode=url`; file and literal pass through unchanged.

If `mode == "url"`: fetch with WebFetch (Claude Code) / `web_fetch` (Gemini) /
curl-via-Bash (Codex) / `webfetch` (OpenCode). If the text is under 500
characters or is clearly a login wall, ask the student to paste the JD and treat
the response as `mode: "literal"`.

**Language detection.** If the JD isn't English, block: "resumasher supports
English JDs only. Detected: <lang>. Please paste an English translation and
retry." (Use your own judgment — no external detector needed.)

Locate the resume:

```bash
RESUME_PATH=$("$RS" orchestration discover-resume "$STUDENT_CWD")
```

`discover-resume` checks, in priority order: `resume.md`, `resume.markdown`,
`cv.md`, `CV.md`, `resume.pdf`, `Resume.pdf`, `cv.pdf`, `CV.pdf`. Markdown wins
over PDF when both exist.

**If `$RESUME_PATH` is empty:** the fast path missed. Don't halt — a student
whose resume is `Lebenslauf.md`, `curriculum.md`, `履歴書.md`, or
`my_resume_final_v3.md` is still a valid user. Ask:

> I couldn't find a resume with one of the default filenames (resume.md, cv.md, resume.pdf, etc.) in this folder. What's the filename? Examples: `Lebenslauf.md`, `履歴書.md`, `my_resume.pdf`.

Validate the answer:

```bash
RESUME_PATH=$("$RS" orchestration validate-resume-path "$STUDENT_CWD" "$STUDENT_ANSWER")
```

Exits 0 printing the absolute path, or exits 1 printing `FAILURE: <reason>` to
stderr (missing, wrong extension, is a directory, unreadable). Re-ask with a
clearer error — e.g. "That file (`notes.docx`) has an unsupported extension.
resumasher accepts `.md`, `.markdown`, and `.pdf`." Up to 3 attempts, then halt:

> resumasher needs a resume to work with. Please add a `.md`, `.markdown`, or `.pdf` file to this folder and try again. You can use the skill's GOLDEN_FIXTURES/resume.md as a template.

Then read it:

```bash
"$RS" orchestration read-resume "$RESUME_PATH" > "$RUN_DIR/resume.txt"
```

---

### Phase 2 — Folder mine (and GitHub mine, if configured)

Resolve the GitHub username. Precedence: `--github <user>` flag > `github_username` in config > empty.

```bash
GITHUB_USER="${GITHUB_FLAG:-$(jq -r '.github_username // ""' "$STUDENT_CWD/.resumasher/config.json" 2>/dev/null || echo "")}"
```

If set, the mine phase mixes GitHub evidence into the folder-miner's context —
no separate sub-agent, the folder-miner prompt already handles both block types.
GitHub mining has its own 1-hour cache under `.resumasher/github-cache/<username>.json`.

```bash
FOLDER_HASH=$("$RS" orchestration folder-state-hash "$STUDENT_CWD")
CACHE_PATH="$STUDENT_CWD/.resumasher/cache.txt"
CACHE_HASH_PATH="$STUDENT_CWD/.resumasher/cache.hash"

if [ -f "$CACHE_HASH_PATH" ] && [ "$(cat "$CACHE_HASH_PATH")" = "$FOLDER_HASH" ] && [ -f "$CACHE_PATH" ] && [ -z "$GITHUB_USER" ]; then
  # Cache hit only applies when GitHub is NOT configured. With GitHub enabled we
  # always re-run mine-context, because GitHub activity changes independently of
  # local folder state (the github-cache TTL inside github_mine.py handles that).
  echo "Folder mine cache hit"
else
  if [ -n "$GITHUB_USER" ]; then
    "$RS" orchestration mine-context "$STUDENT_CWD" \
      --github-username "$GITHUB_USER" > "$RUN_DIR/context.txt"
  else
    "$RS" orchestration mine-context "$STUDENT_CWD" > "$RUN_DIR/context.txt"
  fi
  # Dispatch the folder-miner (below), save its prose to $CACHE_PATH and the hash to $CACHE_HASH_PATH.
fi
```

**GitHub mine failure modes** (all non-fatal — the run continues without GitHub
evidence): rate limit, username not found, network error. Each prints a
`GITHUB_MINE_WARNING` to stderr. To force-refresh, delete
`.resumasher/github-cache/<username>.json` and rerun.

**Build the prompt and dispatch:**

```bash
PROMPT=$("$RS" orchestration build-prompt --kind folder-miner --cwd "$STUDENT_CWD")
```

The compiled prompt wraps `$RUN_DIR/context.txt` in
`<<<FOLDER_CONTEXT_BEGIN>>>/<<<FOLDER_CONTEXT_END>>>` markers with tool-usage
constraints and injection defenses, and asks for a 400-800 word prose summary.

**Retry budget:** folder-miner is load-bearing. If the output starts with
`FAILURE: ` or is empty, retry up to 2 more times (3 total). If all 3 fail, stop:

> Evidence extraction failed after 3 attempts. Please run /resumasher again, or paste your project list manually into `resume.md` and retry.

Cache the successful summary. **Save the sub-agent's text via the Write tool,
OR a heredoc with a quoted delimiter** (`<< 'HEREDOC'`) — never by assigning the
response to a single-quoted shell variable and echoing it. Single-quoted shell
assignment cannot contain a literal `'`; the moment the text contains
`Ana's capstone`, zsh dies with `unmatched '` and the cache is left empty.

```bash
cat > "$CACHE_PATH" << 'HEREDOC'
<paste the sub-agent's text response here>
HEREDOC

echo "$FOLDER_HASH" > "$CACHE_HASH_PATH"
```

On hosts with a Write tool (Claude Code, OpenCode), use Write directly — it
doesn't go through a shell at all, so there's no quoting hazard. **Avoid**
`FOLDER_SUMMARY='...'; echo "$FOLDER_SUMMARY" > file`.

---

### Phase 3 — Company + role, research, output dir

**Extract the company, role, and screening terms.** Dispatch the
`job-extractor` sub-agent — it reads only the JD and returns five lines.
Cheap, and its output does double duty: it names the output directory AND
supplies the exact keyword surface forms the tailor and cover-letter need:

```bash
PROMPT=$("$RS" orchestration build-prompt --kind job-extractor --cwd "$STUDENT_CWD")
```

**Pipe the response through `extract-job-fields`** — do NOT write the per-field
files manually with `echo`. The extractor handles markdown-bold variants
(`**ROLE:** Data Analyst`) that a hand-written `grep` misses, splits the
pipe-separated term lists correctly (requirement strings contain commas, so a
comma split would shred them), and the per-field files let later Bash calls
read values back without shell-source hazards:

```bash
mkdir -p "$RUN_DIR/job"
echo "$EXTRACTOR_OUTPUT" | "$RS" orchestration extract-job-fields --output-dir "$RUN_DIR/job"

COMPANY=$(cat "$RUN_DIR/job/company.txt")
ROLE=$(cat "$RUN_DIR/job/role.txt")
```

That writes `company.txt`, `role.txt`, `hard-requirements.txt`,
`preferred.txt`, `title-variants.txt`, and `keywords.txt`. **`keywords.txt` is
load-bearing** — `build-prompt` reads it for the `jd_keywords` variable in both
the tailor and cover-letter kinds, and those builds exit 2 without it.

Why this exists: applicant tracking systems parse the resume into structured
fields and match them against the requisition string by string. Some enterprise
configurations score an acronym and its expansion as two unrelated skills, so a
resume saying "AWS" can score zero against a posting asking for "Amazon Web
Services." Extracting the JD's exact surface forms once, up front, lets the
tailor spell matched terms the way the screener expects instead of guessing
mid-rewrite.

**Do NOT improvise an env-file heredoc + `source` pattern.** Unquoted
`COMPANY=Elevation Capital` on its own line, then `. job.env`, makes bash parse
`Capital` as a command and leaves `COMPANY` empty whenever the name has a space.
`$(cat file)` strips the trailing newline but preserves every interior character
byte-perfect.

If `COMPANY` is empty, ask once: "I couldn't identify the company from the JD.
What company is this role at?"

**Compute the output directory and persist the path** so later Bash calls can read it back:

```bash
SLUG=$("$RS" orchestration company-slug "$COMPANY")
DATE=$(date +%Y%m%d)
OUT_DIR="$STUDENT_CWD/applications/$SLUG-$DATE"
mkdir -p "$OUT_DIR"
echo "$OUT_DIR" > "$RUN_DIR/out-dir.txt"
cp "$RUN_DIR/jd.txt" "$OUT_DIR/jd.md"
```

The `cp` persists the JD (with its Source URL header) into the application
folder. `$RUN_DIR` is wiped at the start of every run, so without this copy the
JD is lost as soon as the student runs against a different posting.

**Company research.** Dispatch the `company-researcher` sub-agent:

```bash
PROMPT=$("$RS" orchestration build-prompt --kind company-researcher --cwd "$STUDENT_CWD" --company "$COMPANY")
```

This sub-agent MUST have web search available — `WebSearch`/`WebFetch` (Claude
Code), `web_search`/`web_fetch` (Gemini), `web_search` opt-in (Codex),
`websearch`/`webfetch` (OpenCode, needs `OPENCODE_ENABLE_EXA=1` or the OpenCode
provider). That's the whole point of the task.

Save the research to `$OUT_DIR/company-research.md` — the cover-letter
`build-prompt` reads it from there. Same heredoc-or-Write rule; company facts
routinely contain possessives (`OpenAI's funding round`) that break single-quoted
shell assignment.

```bash
cat > "$OUT_DIR/company-research.md" << 'HEREDOC'
<paste the company-researcher sub-agent's text response here>
HEREDOC
```

If the sub-agent returns a FAILURE sentinel, ask the student: "Company research
failed (<reason>). Paste 2-3 bullets of what you already know about {company},
or leave blank to accept a generic cover letter." Write whatever they give you
(or a one-line "no research available") to the same path so the next phase can
still build its prompt.

---

### Phase 4 — Tailor

```bash
PROMPT=$("$RS" orchestration build-prompt --kind tailor --cwd "$STUDENT_CWD")
```

The compiled prompt carries the full tailoring spec — schema, section order per
market, bullet ranking and bullet marker, length targets, multi-role tenure format,
`[INSERT ...]` placeholder rules, the SOFT-alternate requirement, and the
non-negotiable ANCHORING RULE forbidding fabricated experience to match the JD.
It also contains a pre-built contact header read from `.resumasher/config.json`,
which the tailor copies verbatim rather than inferring contact details from a
possibly-stale resume. Template: `scripts/prompts.py`, `tailor` kind — the
canonical source. Edits go there, not here.

Save to `$OUT_DIR/tailored-resume.md` — same heredoc-or-Write rule. The tailored
resume is dense with possessives, single-quoted clauses, dollar signs in metrics
($2M, $500K), and backticks in technical bullets.

```bash
cat > "$OUT_DIR/tailored-resume.md" << 'HEREDOC'
<paste the tailor sub-agent's text response here>
HEREDOC
```

**Retry budget:** 1 retry. If the retry also fails, hard-stop — the tailored
resume is the core deliverable and a stub isn't acceptable.

**Placeholders stay in the file.** The tailor emits `[INSERT TEAM SIZE]`-style
tokens where the evidence didn't supply a metric, each paired with a
`<!--SOFT: ... -->` no-metric alternate on the same line. **Do NOT walk the
student through filling these interactively.** They are editing this markdown
themselves before sending; filling a metric in their own editor, with the whole
document visible, beats answering one bullet at a time in a terminal prompt.
Leave both versions in place and surface the count in the summary.

---

### Phase 5 — Cover letter, cleanup, summary

```bash
PROMPT=$("$RS" orchestration build-prompt --kind cover-letter --cwd "$STUDENT_CWD" --out-dir "$OUT_DIR")
```

The compiled prompt reads `$OUT_DIR/tailored-resume.md`, `$RUN_DIR/jd.txt`,
`$RUN_DIR/job/keywords.txt`, `$OUT_DIR/company-research.md`,
`.resumasher/cache.txt`, and the config (contact header + `relocation_context`).
It produces a classic European motivation letter: H1 name + contact line
(verbatim from config), today's date, company name, an `Attn:` hiring-contact
line, `**Re:** {Position}` subject, named greeting, 3-4 body paragraphs,
`Sincerely,`, printed name. The date is pre-formatted by the orchestrator
(`May D, YYYY`) so the model can't drift.

Three things about this letter differ from a generic one, and all three are
enforced in the prompt (`scripts/prompts.py`, `cover-letter` kind):

- **Paragraph 2 is a story, not a skills list.** It follows a causal chain —
  a specific situation the candidate hit during a project or internship, the
  conclusion they drew from it, and why that makes a specific responsibility
  in *this* posting interesting. It must come from the resume or evidence
  blocks; inventing an experience to make a better story is forbidden.
- **A named recipient.** If the JD gives no contact, the letter carries an
  `[INSERT HIRING MANAGER OR RECRUITER NAME ...]` placeholder with LinkedIn
  lookup instructions rather than "To Whom It May Concern".
- **A relocation sentence, only when `relocation_context` is set.** It gives
  a concrete reason for the country or city and states work authorization
  using *only* the facts in the config string.

Save the sub-agent's text response to `$OUT_DIR/cover-letter.md` via Write or a
quoted heredoc. Cover letters routinely contain possessives (`the company's
mission`, `we're building`) that break `VAR='...'` with `unmatched '` and
silently produce empty files.

```bash
cat > "$OUT_DIR/cover-letter.md" << 'HEREDOC'
<paste the cover-letter sub-agent's text response here>
HEREDOC
```

The sub-agent was told not to write files itself. If it disobeyed, ignore the
file it wrote and use the text response from its message.

**Strip em dashes — this step is mandatory, not conditional.** The prompt
forbids them, but a prompt is a request and this requirement is absolute. Run
the rewrite on both documents immediately after saving them:

```bash
"$RS" orchestration sanitize-dashes --input "$OUT_DIR/cover-letter.md"
"$RS" orchestration sanitize-dashes --input "$OUT_DIR/tailored-resume.md"
```

It replaces each em dash with a comma (or an en dash between digits, where the
dash was a numeric range) and prints every line it rewrote. **Read those lines
back.** A comma occasionally lands where a period would have read better; fix
those with the Edit tool. Do not skip the re-read, and do not skip the command
because the letter "looks fine" — you cannot reliably spot an em dash by
eye in a terminal.

**Normalize the resume's bullet markers — also mandatory.** Every bullet in the
tailored resume ships as `• `, never `- `. The prompt asks for it; this makes it
true:

```bash
"$RS" orchestration bulletize --input "$OUT_DIR/tailored-resume.md"
```

It rewrites leading `- ` / `* ` / `+ ` list markers only. Hyphens inside bullet
text ("A/B-tested", "end-to-end") and `---` rules are left alone, so there is
nothing to re-read afterwards. Resume only — the cover letter is prose and has
no bullets to convert.

**Retry budget:** 1 retry. On second failure write a stub and continue — the
student still gets the resume:

```
# Cover Letter — generation failed

This document was not generated. Re-run /resumasher <job-source> to regenerate it.
```

**Sweep stray prompt-staging files** left in `/tmp` by an agent that improvised
around the `$RUN_DIR/prompts/` prescription. Those files contain student PII
(resume, JD, project content), and on macOS `/tmp` is world-readable to other
local users until reboot:

```bash
START_TS=$(cat "$RUN_DIR/start-ts.txt")
"$RS" orchestration cleanup-stray-prompts --since-timestamp "$START_TS"
```

The scan is narrow by design: `/tmp` only (no recursion, never outside `/tmp`),
only basenames matching `<kind>-prompt.{txt,md}` for a registered kind, only
files newer than `$START_TS`.

**Append the history record:**

```bash
OUT_DIR=$(cat "$RUN_DIR/out-dir.txt")
COMPANY=$(cat "$RUN_DIR/job/company.txt")
"$RS" orchestration append-history "$STUDENT_CWD" "$(cat <<EOF
{
  "ts": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "company": "$COMPANY",
  "output_dir": "$OUT_DIR",
  "errors": []
}
EOF
)"
```

**Run the two deterministic checks.** Both are pure Python — no LLM, no
sub-agent, no token cost. Run them and put their output in the summary
verbatim; do not paraphrase, and do not decide on the student's behalf which
findings matter.

```bash
echo "--- screening term coverage ---"
"$RS" orchestration keyword-coverage \
    --job-dir "$RUN_DIR/job" \
    --resume "$OUT_DIR/tailored-resume.md"

echo "--- resume check ---"
"$RS" orchestration lint-output --input "$OUT_DIR/tailored-resume.md" --kind resume

echo "--- cover letter check ---"
"$RS" orchestration lint-output --input "$OUT_DIR/cover-letter.md" --kind cover-letter

echo "--- em dash guarantee (must report zero) ---"
"$RS" orchestration sanitize-dashes --input "$OUT_DIR/cover-letter.md" --check
"$RS" orchestration sanitize-dashes --input "$OUT_DIR/tailored-resume.md" --check

echo "--- bullet marker guarantee (must report zero) ---"
"$RS" orchestration bulletize --input "$OUT_DIR/tailored-resume.md" --check
```

`--check` reports without rewriting and exits 1 if it finds anything. After
Phase 5's mandatory sanitize pass, both should print "no em dashes". If either
does not, the sanitize step was skipped — run it without `--check` now. The
same holds for `bulletize --check`: it should report that every bullet already
starts with •.

`keyword-coverage` reports which of the JD's required and preferred terms
appear in the tailored resume. **A missing term is not automatically a bug.**
It is missing for one of two reasons: the candidate genuinely lacks that
experience (correct — the anchoring rule forbids inventing it, and no amount of
keyword coverage is worth a fabricated bullet), or the tailor described real
experience using different words (fixable, and worth telling the student). Say
which you think it is for each missing term, and never suggest adding a term
the evidence doesn't support.

`lint-output` flags unresolved `[INSERT ...]` placeholders, leftover
`<!--SOFT: ...-->` comments, em dashes, and phrasing recruiters report as
machine-written. All warnings, never blocking — some flagged words are the
right word in context, and that call belongs to the student.

**Count the remaining placeholders** so the summary can point at them:

```bash
count_placeholders() {
  if [ -f "$1" ]; then
    # grep -c prints "0" on no-match (exit 1) or "N" on match (exit 0).
    # `|| true` swallows the exit code without appending a second "0" —
    # `|| echo 0` would produce "0\n0" for the zero-match case.
    grep -c '\[INSERT' "$1" 2>/dev/null || true
  else
    echo 0
  fi
}
PH_RESUME=$(count_placeholders "$OUT_DIR/tailored-resume.md")
PH_COVER=$(count_placeholders "$OUT_DIR/cover-letter.md")
```

Print a short summary:

```
resumasher run complete.

Company: {company}
Role:    {role}
Output:  {out_dir}

  ✓ tailored-resume.md
  ✓ cover-letter.md
  ✓ jd.md (the posting, for your records)

Screening terms: {matched}/{total} of the posting's required terms appear
in your resume.
```

Then list any missing terms with your read on each — "you don't have this"
versus "you have this, it's just worded differently, here's the line to
change." If `lint-output` returned findings, list them under a short heading
and say plainly that they're suggestions.

If `PH_RESUME > 0` or `PH_COVER > 0`, add:

```
✏️  {N} bullet(s) need a number from you. Search for "[INSERT" in
   tailored-resume.md — each one sits on a line that also carries a
   <!--SOFT: ... --> alternate. Three options per bullet:
     • fill in the real number and delete the SOFT comment
     • delete the [INSERT] version and keep the SOFT text
     • drop the bullet entirely
   Don't ship a resume with "[INSERT" still in it — and delete the
   <!--SOFT: ...--> comments too. They're invisible in a markdown
   preview but paste into Word as visible text.
```

Then:

```
Next steps:
  1. Open tailored-resume.md and read it end to end. Check the section order
     and the bullet ranking — the top bullet in each role should be the one
     this JD cares most about.
  2. Read cover-letter.md paragraph 2 carefully; the AI sometimes overstates.
  3. Paste both into Word / Google Docs / Pages and format them. Export to
     PDF from there before submitting.

Applied through Workday or Greenhouse? Run your formatted PDF through
jobscan.co (free preview) with this JD pasted in, and verify the sections
parse cleanly before sending.
```

---

## Error recovery

- folder-miner, tailor: hard-stop after the retry budget.
- job-extractor, company-researcher, cover-letter: continue degraded (ask the student, or write a stub).

Always give the student a status summary explaining what succeeded, what failed,
and a concrete retry command for each failed artifact.

## Re-running after manual edits

If the student says "I edited tailored-resume.md, regenerate the cover letter"
or similar: do NOT re-run `/resumasher <job>` from scratch — that re-dispatches
every sub-agent and overwrites their edits. Jump to the single phase they asked
for, with `$OUT_DIR` pointing at the existing application folder. The student's
manual edits are authoritative.
