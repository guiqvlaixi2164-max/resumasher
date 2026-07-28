# resumasher

[![CI](https://github.com/earino/resumasher/actions/workflows/ci.yml/badge.svg)](https://github.com/earino/resumasher/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/earino/resumasher/blob/main/LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 199 passing](https://img.shields.io/badge/tests-199%20passing-brightgreen.svg)](https://github.com/earino/resumasher/tree/main/tests)

resumasher tailors your resume and writes a matching cover letter for a specific job. It runs as an [Agent Skill](https://github.com/anthropics/skills) inside your AI CLI (**Claude Code**, **OpenAI Codex CLI**, **Google Gemini CLI**, or **OpenCode**), reading your actual work to back every claim with concrete evidence.

**It outputs markdown, not PDFs.** The text is the hard part — the wording, the evidence, which experience leads. Formatting is the easy part, and you'll want to do it in Word or Google Docs anyway. resumasher hands you clean, tailored markdown and gets out of the way.

![resumasher running: terminal walkthrough from `/resumasher job.md` through the folder mine, tailor, and cover letter](assets/img/demo.gif)

## Quick install

Paste this into Claude Code, Codex CLI, Gemini CLI, or OpenCode:

> Install the resumasher skill available at https://github.com/earino/resumasher

The AI CLI reads the README, picks the right path for your CLI, clones, and runs the installer. For exact per-CLI commands or project-scope install, see [Install](#install) below.

## What you get

From your resume folder, run:

```bash
/resumasher job.md
```

A couple of minutes later you get `./applications/<company>-<date>/` containing:

| File | What it is |
|---|---|
| `tailored-resume.md` | Your resume rewritten for this JD — bullets rewritten around the evidence, sections ordered for the market, bullets ranked by relevance to the posting |
| `cover-letter.md` | ~300 words in 3 paragraphs, weaving in recent company news with citations |
| `jd.md` | The posting as ingested, for your records |

Open the markdown in any editor, fill in the handful of `[INSERT ...]` metrics
it flags, paste into Word / Google Docs / Pages, format, export to PDF, send.

## The unfair advantage: it sees your actual work

Every other resume-tailoring tool is a web app that only sees the summary you paste in. resumasher runs inside your AI CLI, so it pulls from two evidence sources the web tools cannot reach:

**Your public GitHub.** One-time setup, then every run mines your non-fork repos: names, descriptions, topics, README content, last-push date. For most students this is where the evidence lives, especially on a borrowed or clean laptop.

**Your working directory.** If you keep project files locally (capstone code, ML notebooks, text-mining writeups, PDF reports), resumasher reads those too and cites specific files.

Your bullet becomes: "Built an XGBoost churn classifier on 2.3M rows, F1=0.82, deployed to Flask. See `github.com/you/churn-model`" instead of "built a machine learning model."

## Install

resumasher is an [Agent Skills](https://github.com/anthropics/skills) package. If your AI CLI asks "is this a plugin," the answer is no, it's a skill. Each host has its own skill directory convention (`.claude/skills/`, `.codex/skills/`, `.gemini/skills/`, `.opencode/skills/`) but the skill source is identical. Pick the block that matches your AI CLI.

**⚠️ `install.sh` is mandatory on every host.** `git clone` alone only copies files. It does NOT create the Python virtual environment or install the required packages (pdfminer.six, chardet, nbconvert). If you skip `install.sh`, the next invocation of `/resumasher` will crash with `ModuleNotFoundError: No module named 'pdfminer'` and you'll think the skill is broken.

### Claude Code

**User-scope, recommended** (skill available in every folder):

```bash
git clone https://github.com/earino/resumasher.git ~/.claude/skills/resumasher
bash ~/.claude/skills/resumasher/install.sh
```

**Project-scope** (skill available only in the current folder — use when you want the skill checked in alongside a specific job-search project):

```bash
git clone https://github.com/earino/resumasher.git .claude/skills/resumasher
bash .claude/skills/resumasher/install.sh
```

Restart Claude Code, then run `/resumasher <job>` from a folder with your `resume.md` or `resume.pdf`.

### OpenAI Codex CLI

**User-scope, recommended:**

```bash
git clone https://github.com/earino/resumasher.git ~/.codex/skills/resumasher
bash ~/.codex/skills/resumasher/install.sh
```

**Project-scope** (only when you want the skill scoped to one folder):

```bash
git clone https://github.com/earino/resumasher.git .codex/skills/resumasher
bash .codex/skills/resumasher/install.sh
```

Restart Codex, then run `/resumasher <job>` from a folder with your `resume.md` or `resume.pdf`.

### Google Gemini CLI

Gemini CLI has a first-class `skills install` subcommand that handles the clone for you:

```bash
gemini skills install --user https://github.com/earino/resumasher    # user-scope, recommended
gemini skills install https://github.com/earino/resumasher           # project-scope (only when scoped to one folder is what you want)
```

Gemini will prompt you to confirm before installing. After it finishes, run the Python installer once:

```bash
bash ~/.gemini/skills/resumasher/install.sh        # user-scope
bash .gemini/skills/resumasher/install.sh          # project-scope
```

Restart Gemini, then run `/resumasher <job>` from a folder with your `resume.md` or `resume.pdf`.

### OpenCode

OpenCode reads `~/.claude/skills/` natively as a Claude-compat directory, so the simplest install is the Claude Code block above — clone to `~/.claude/skills/resumasher/` and OpenCode picks it up automatically. If you'd rather use OpenCode's native skills directory:

```bash
git clone https://github.com/earino/resumasher.git ~/.opencode/skills/resumasher
bash ~/.opencode/skills/resumasher/install.sh
```

Or project-scope (recommended only when this resume folder is the only place you'll use the skill):

```bash
git clone https://github.com/earino/resumasher.git .opencode/skills/resumasher
bash .opencode/skills/resumasher/install.sh
```

Restart OpenCode, then run `/resumasher <job>` from a folder with your `resume.md` or `resume.pdf`. Requires OpenCode v1.0.110+ for native skill discovery (use `opencode --version` to check).

`install.sh` automatically drops the slash-command shim at `~/.config/opencode/commands/resumasher.md` when it detects the `opencode` binary on PATH. The shim wires `/resumasher <args>` to invoke the skill — without it, OpenCode just pastes SKILL.md as a user message and drops the argument. If you skip the installer, copy `commands/resumasher.md` from this repo to `~/.config/opencode/commands/resumasher.md` manually.

#### OpenCode `tool_output.max_bytes` setting (small-model users only)

OpenCode caps tool output at 51,200 bytes by default ([source](https://github.com/sst/opencode/blob/dev/packages/opencode/src/tool/truncate.ts)). resumasher's `SKILL.md` is ~28KB, comfortably under the default. But if you've *lowered* the cap, OpenCode truncates the skill load and the model sees only part of the workflow — and a model that reads half the pipeline ships broken output rather than failing loudly. Strong cloud models (Claude, GPT-5) usually recover by inferring the missing phases; weak local models (qwen-32b, llama-32b) do not.

`install.sh` reads your OpenCode config (read-only, never modifies it) and prints a heads-up if your cap is below `SKILL.md`'s size. To raise it, add this to `~/.config/opencode/opencode.json` (or the `XDG_CONFIG_HOME`-based equivalent):

```json
{
  "tool_output": {
    "max_bytes": 102400
  }
}
```

100KB is double the default and comfortably fits resumasher today plus headroom for future SKILL.md growth. If you only run resumasher under cloud models you can skip this — the truncation degrades gracefully on Claude/GPT-5.

### Verify the install

From a fresh AI CLI session, try the bundled fixtures:

```bash
cd <install-dir>/GOLDEN_FIXTURES    # e.g. ~/.claude/skills/resumasher/GOLDEN_FIXTURES
/resumasher sample-jd.md
```

A couple of minutes later you should see `tailored-resume.md` and `cover-letter.md` in `./applications/deloitte-consulting-<today>/`. Wall-clock time depends on the LLM in use, GitHub fetch latency, and your network.

### For your AI CLI: authoritative install instructions

<details>
<summary>Expand (instructions for AI CLIs reading this page. Students can skip.)</summary>

**If a user asks their AI CLI (Claude Code, Codex, Gemini, or OpenCode) to install this skill, the CLI should follow the matching block above.** Pick the block for your host, run both commands, done. Do not attempt to install this as a Claude Code plugin. There is no `.claude-plugin/` manifest and the repo is not a plugin marketplace. It's a skill. The skill directory conventions differ per host but the skill source is identical.

</details>

## Usage

### Invocation

Three input forms, all work:

```bash
/resumasher job.md                                               # local file
/resumasher https://careers.deloitte.com/job/12345               # URL
/resumasher "Senior Data Analyst at Acme Corp. Requirements: SQL, Python..."  # literal text
```

### First-run setup (one time per folder)

The first time you run `/resumasher` in a folder, it reads your contact details off your resume, shows you what it found, and asks you to confirm — one question, not five. It also asks once whether you have a GitHub to mine. Everything is stored locally in `.resumasher/` and nothing is uploaded.

### Accepted resume formats

resumasher looks for these files in the working directory, in priority order:

1. `resume.md` / `resume.markdown`
2. `cv.md` / `CV.md`
3. `resume.pdf` / `Resume.pdf`
4. `cv.pdf` / `CV.pdf`

**Markdown is preferred** because it's the source-of-truth you should be editing anyway (diff-friendly, easy to update, no rendering stack needed). If both a `.md` and a `.pdf` exist, the `.md` wins.

**PDF works if that's all you have.** resumasher extracts the selectable text via `pdfminer.six` and hands it to the tailor sub-agent. Caveats:

- Scanned / image-only PDFs will fail with a clear error. resumasher does not OCR.
- PDF text extraction loses some structure (columns, tables). The tailor will restructure it, but results are cleaner from a `resume.md`.
- If you want to keep iterating, export your `tailored-resume.md` from the first run as your new base. Future runs will be markdown-driven.

### Folder layout

```
my-job-search/
├── resume.md            # Your base resume (see formats above)
├── applications/        # resumasher writes tailored markdown here
└── projects/            # Your work: code, notebooks, READMEs, PDFs
    ├── capstone/
    ├── ml-final/
    └── text-mining/
```

See `GOLDEN_FIXTURES/` in this repo for a full example.

### Iterating in the same folder

Each run's JD file sits alongside your resume. If you apply to several roles from one folder, delete or archive the old JD file before the next run, or put each JD in its own subfolder. Otherwise the folder miner picks up every JD you've tried and hands them to the tailor as context, wasting tokens and confusing the sub-agent.

### GitHub profile (optional, auto-used when configured)

If your work lives on GitHub more than on your current laptop, or you're applying from a borrowed machine, resumasher can mine your public GitHub profile for evidence. Setup is one prompt at first-run: *"Do you have a GitHub? We can leverage it for this."* Paste your username (or a profile URL, we strip the prefix), and every subsequent run automatically mixes your repos into the evidence pool.

What resumasher fetches per repo: name, description, topics, primary language, last push date, stargazer count, README content (up to 50KB).

What it skips: forks, archived repos, empty repos, source code (too noisy), issues, PRs, contribution graphs. Default cap is 15 most-recently-pushed repos.

**Auth and rate limits.** resumasher uses the GitHub CLI (`gh api`) if it's installed and authenticated, giving you a 5000/hour rate limit and reusing your existing auth with zero PAT handling. Without `gh`, it falls back to unauthenticated requests (60/hour), enough for small profiles but tight for anything bigger. If you hit the limit, resumasher prints a clear message and continues without GitHub evidence. To unlock the 5000/hour limit:

```bash
brew install gh   # or see https://cli.github.com
gh auth login
```

**One-off override.** For a borrowed laptop or an alternate account, pass `--github <username>` on the command line. It beats whatever's in your config for that single run.

**Caching.** GitHub responses are cached for 1 hour under `.resumasher/github-cache/<username>.json`. Iterate on the same JD multiple times without re-hitting the API. Delete the file to force a refresh.

### Flags

```bash
/resumasher <job> --github <username>   # One-run override of the configured GitHub account
```

Section order is inferred from the JD's market rather than set by a flag:
anglophone postings get Summary → Experience → Projects → Skills → Education;
continental-European postings move Education above Skills. Override it by
reordering the markdown yourself — it's your file.

## Updating an existing install

Three commands in the skill's install directory: `git pull` to fetch new code, `bash install.sh` to refresh the venv if `requirements.txt` changed (idempotent if it didn't), then restart the AI CLI so the updated `SKILL.md` gets picked up.

Pick the block matching the AI CLI you're running in. Each block prefers the user-scope install (`~/.<host>/skills/`) and falls back to project-scope (`.<host>/skills/`) if only the latter exists.

### Claude Code

```bash
if [ -d ~/.claude/skills/resumasher/.git ]; then
  cd ~/.claude/skills/resumasher
elif [ -d .claude/skills/resumasher/.git ]; then
  cd .claude/skills/resumasher
else
  echo "resumasher is not installed for Claude Code. See Install section above."; exit 1
fi
git pull
bash install.sh
```

Then restart Claude Code.

### OpenAI Codex CLI

```bash
if [ -d ~/.codex/skills/resumasher/.git ]; then
  cd ~/.codex/skills/resumasher
elif [ -d .codex/skills/resumasher/.git ]; then
  cd .codex/skills/resumasher
else
  echo "resumasher is not installed for Codex. See Install section above."; exit 1
fi
git pull
bash install.sh
```

Then restart Codex.

### Google Gemini CLI

```bash
if [ -d ~/.gemini/skills/resumasher/.git ]; then
  cd ~/.gemini/skills/resumasher
elif [ -d .gemini/skills/resumasher/.git ]; then
  cd .gemini/skills/resumasher
else
  echo "resumasher is not installed for Gemini. See Install section above."; exit 1
fi
git pull
bash install.sh
```

Then restart Gemini.

### OpenCode

If you installed via the Claude Code path (`~/.claude/skills/`), use the Claude Code update block above — OpenCode reads the same directory. For native OpenCode installs:

```bash
if [ -d ~/.opencode/skills/resumasher/.git ]; then
  cd ~/.opencode/skills/resumasher
elif [ -d .opencode/skills/resumasher/.git ]; then
  cd .opencode/skills/resumasher
else
  echo "resumasher is not installed for OpenCode. See Install section above."; exit 1
fi
git pull
bash install.sh
```

Then restart OpenCode.

### For your AI CLI: authoritative update instructions

<details>
<summary>Expand (instructions for AI CLIs reading this page. Students can skip.)</summary>

**When a user asks you (their AI CLI) to update resumasher**, run the detection-and-update bash block above for the AI CLI you are currently running in. Only update the install directory matching the AI CLI you are running in; do not touch installs for other hosts. After the commands complete, tell the user to restart this specific AI CLI (not a generic "restart your AI CLI").

If the detection block exits with "resumasher is not installed," do not guess. Tell the user resumasher is not installed for this AI CLI and point them at the Install section above. Do not silently fall back to installing from scratch.

</details>

## ATS safety

resumasher generates markdown, so ATS safety is mostly in your hands at format time. What the skill controls, it gets right: single-column structure, conventional section headings ATS parsers recognize, one project per heading, no tables or multi-column layouts in the source.

**Before applying through a major ATS** (Workday, Taleo, iCIMS), export your formatted resume to PDF and upload it to [jobscan.co](https://www.jobscan.co/) (free preview) with the JD pasted in, and eyeball that sections parse the way you'd expect. Avoid text boxes, tables, and sidebars when you format — those are the most common cause of word-salad parsing.

## Something looks wrong?

resumasher runs inside your AI CLI, so stay in the same chat and describe what you see in plain English. The agent has the generated markdown, the JD, and the mined evidence right there — it can tell you whether a weak bullet came from thin evidence or from the tailor underselling you, and rewrite it.

## Architecture

The skill runs a five-phase pipeline: first-run setup → intake → folder + GitHub mine → company/role extract + research → tailor → cover letter.

Sub-agents dispatch via each host's subagent mechanism (Claude's `Task` with `subagent_type="general-purpose"`, Gemini's `@generalist`, Codex's inline execution, or OpenCode's `task` with `subagent_type="general"`). Interactive prompts use each host's native tool (`AskUserQuestion` / `request_user_input` / `ask_user` / `question`) with a hard-fail fallback for non-interactive contexts.

The LLM pipeline runs prose between phases (no JSON), with small sentinel lines (`COMPANY: Deloitte`, `ROLE: Data Analyst`, `FAILURE: ...`) where structure actually matters. Job descriptions and company-research output are wrapped in `<<<UNTRUSTED_*>>>` markers before reaching sub-agents with file or web access. Basic prompt-injection containment.

```
resumasher/
├── SKILL.md                # Orchestration prompt the AI CLI follows at runtime
├── bin/
│   └── resumasher-exec     # Self-locating wrapper around venv Python
├── scripts/
│   ├── orchestration.py    # Deterministic helpers (CLI + importable)
│   ├── prompts.py          # All 5 sub-agent prompt templates + substitution
│   └── github_mine.py      # GitHub profile evidence fetcher
├── GOLDEN_FIXTURES/        # Sample portfolio for testing and demo
├── tests/                  # pytest suite
├── install.sh              # One-liner installer + venv setup
└── requirements.txt
```

## Development

```bash
# Run the test suite (199 tests, ~5 seconds)
source .venv/bin/activate
pytest tests/ -v

# Try the skill on the bundled fixtures
cd GOLDEN_FIXTURES
/resumasher sample-jd.md
```

Before opening a PR:

- `pytest tests/ -v` should pass.
- If you change a sub-agent prompt, run it against `GOLDEN_FIXTURES/` and read the output. Prompt regressions don't show up in unit tests.
- Prompt templates live in `scripts/prompts.py` — that's the canonical source. `SKILL.md` describes the orchestration, not the prompt text.

## Roadmap

**Shipped:**
- Markdown-first output — tailored resume + cover letter, no PDF renderer
- Market-aware section ordering and JD-relevance bullet ranking, decided by the tailor
- English-only JD input (pasted, file, or URL)
- Five-phase pipeline with prompt-injection containment
- Multi-role tenures rendered correctly (e.g. a Meta progression as one company entry with sub-role bullets)
- `resume.pdf` accepted when no markdown source exists
- Non-English resume filenames (`Lebenslauf.md`, `履歴書.md`, `my_resume_final_v3.md`) — when auto-discovery misses, the skill asks once and validates the answer
- GitHub profile mining (`gh api` preferred, unauthenticated fallback)
- `[INSERT ...]` placeholders paired with `<!--SOFT: ... -->` alternates, left in the file for you to resolve while editing
- Local application history log (`.resumasher/history.jsonl`)
- Runs on Claude Code, OpenAI Codex CLI, Google Gemini CLI, and OpenCode
- GitHub Actions CI on Python 3.10, 3.11, 3.12

**Planned:**
- `--review` mode: step-by-step interactive rewriting for every bullet ([#11](https://github.com/earino/resumasher/issues/11))
- Final coherence pass flagging drift between resume and cover letter ([#1](https://github.com/earino/resumasher/issues/1))
- Incremental folder-mine cache invalidation ([#10](https://github.com/earino/resumasher/issues/10))
- German / French JD translation pre-pass ([#7](https://github.com/earino/resumasher/issues/7))
- Facts persistence: remember placeholder answers across runs ([#9](https://github.com/earino/resumasher/issues/9))

## Contributing

PRs and issues welcome. resumasher is explicitly shaped by feedback from early users: what surprised you, what looked wrong, what you wish the tool had caught. File anything that helped or bit you.

## License

MIT. See [LICENSE](https://github.com/earino/resumasher/blob/main/LICENSE). Fork it, extend it, ship it to your students.

## Credits

Built by [Eduardo Ariño de la Rubia](https://github.com/earino) for his wonderful students, and anyone else who may find it useful.

Designed with [gstack](https://github.com/garrytan/gstack) (office-hours and plan-eng-review skills) and built with [Claude Code](https://claude.com/claude-code).
