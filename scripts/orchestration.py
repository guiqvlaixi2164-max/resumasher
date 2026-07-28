"""
Deterministic orchestration helpers for resumasher.

Everything that can be done without calling an LLM lives here: parsing args,
finding the resume, hashing folder state, mining content the LLM will see,
regex-extracting the company and role, appending history, and first-run setup.

These are CLI-callable so SKILL.md can shell out without re-implementing logic
in a prompt, and importable so tests can exercise every branch.

CLI map:
    python -m scripts.orchestration parse-job-mode <arg>
    python -m scripts.orchestration parse-job-content <arg>
    python -m scripts.orchestration format-jd --mode <mode> [--url <url>]
    python -m scripts.orchestration discover-resume <cwd>
    python -m scripts.orchestration validate-resume-path <cwd> <filename>
    python -m scripts.orchestration folder-state-hash <cwd>
    python -m scripts.orchestration mine-context <cwd> [--github-username <user>]
    python -m scripts.orchestration read-resume <path>
    python -m scripts.orchestration extract-company <<< "prose with COMPANY: Deloitte line"
    python -m scripts.orchestration extract-role <<< "prose with ROLE: Data Analyst line"
    python -m scripts.orchestration extract-job-fields --output-dir <dir>
    python -m scripts.orchestration keyword-coverage --job-dir <dir> --resume <path>
    python -m scripts.orchestration lint-output --input <path> --kind cover-letter
    python -m scripts.orchestration is-failure <<< "FAILURE: reason"       (exit 0 = yes)
    python -m scripts.orchestration append-history <cwd> <json-line>
    python -m scripts.orchestration first-run-needed <cwd>
    python -m scripts.orchestration ensure-gitignore <cwd>
    python -m scripts.orchestration company-slug "Deloitte Consulting LLC"
    python -m scripts.orchestration build-prompt --kind <kind> --cwd <cwd>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Iterable, Optional

import chardet

# Ensure sibling modules (e.g., github_mine, prompts) import cleanly whether
# this file is run as a script (`python scripts/orchestration.py`) or imported
# as a module (`from scripts import orchestration`). When run as a script,
# Python puts this file's directory on sys.path, so `import github_mine`
# works. When imported as a module (in tests), both the package and the
# scripts dir are on sys.path. The explicit insert below makes the
# script-invocation path bulletproof regardless of caller context.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Import the prompt registry eagerly so the CLI's --kind choices can be
# populated. The prompts module has no heavy deps (just stdlib + dataclasses),
# so the import cost is negligible.
from prompts import (
    PROMPT_KINDS as _PROMPT_KINDS,
    build_prompt as _build_prompt,
    format_contact_info as _format_contact_info,
)


# ---------------------------------------------------------------------------
# 1. parse_job_source: file-path | URL | literal precedence
# ---------------------------------------------------------------------------

JobSourceMode = str  # "file" | "url" | "literal"


@dataclass
class JobSource:
    mode: JobSourceMode
    content: str       # file contents, URL string, or literal text
    path: Optional[str] = None  # set when mode == "file"


def parse_job_source(arg: str, cwd: Optional[Path] = None) -> JobSource:
    """
    Resolve <job-source> to (mode, content). Precedence:
      1. If arg refers to an existing file path -> mode=file, content=file text
      2. Else if arg starts with http:// or https:// -> mode=url, content=arg
      3. Else -> mode=literal, content=arg

    Why the file-first check matters: a user could name a file "https.md". The
    file-existence check wins over URL-lookalike strings.
    """
    cwd = cwd or Path.cwd()
    candidate = cwd / arg if not os.path.isabs(arg) else Path(arg)
    if candidate.exists() and candidate.is_file():
        text = _read_text_with_encoding_detection(candidate)
        return JobSource(mode="file", content=text, path=str(candidate))

    if arg.lower().startswith(("http://", "https://")):
        return JobSource(mode="url", content=arg)

    return JobSource(mode="literal", content=arg)


def format_jd(mode: str, content: str, url: Optional[str] = None) -> str:
    """
    Format the JD text that gets written to $RUN_DIR/jd.txt and subsequently
    copied to $OUT_DIR/jd.md in Phase 3.

    For mode="url", prepend a `Source URL: <url>` header (followed by a blank
    line) so the posting URL survives alongside the fetched page text. A
    recruiter follow-up weeks after the application, or a re-read of the exact
    posting phrasing, both need the URL, and the fetched page text alone drops
    it (the URL is metadata, not content).

    For mode="file" or mode="literal", return content unchanged — there's no
    URL to preserve (file mode: the student already has the file; literal
    mode: the JD was pasted inline).

    If mode="url" is passed but url is None/empty, return content unchanged as
    a defensive fallback — we'd rather ship an un-headered file than crash.
    """
    if mode == "url" and url:
        return f"Source URL: {url}\n\n{content}"
    return content


# ---------------------------------------------------------------------------
# 2. discover_resume: canonical filenames in priority order
# ---------------------------------------------------------------------------

# Markdown is preferred (source-of-truth, easier to diff), then PDF.
# If a student has both resume.md and resume.pdf, the .md wins — most
# students keep the .md as their working copy and export the PDF from it.
RESUME_CANDIDATES = [
    "resume.md", "resume.markdown", "cv.md", "CV.md",
    "resume.pdf", "Resume.pdf", "cv.pdf", "CV.pdf",
]


def discover_resume(cwd: Path) -> Optional[Path]:
    """Return the highest-priority resume-like file at the CWD root, or None.

    Enumerates the directory and matches on lowercased names, so the returned
    Path carries the real on-disk filename even on case-insensitive filesystems
    (macOS APFS, Windows NTFS) — probing `(cwd / "cv.pdf").exists()` there
    matches a file named `CV.pdf` but returns a Path whose `.name` is the
    candidate string, not what's on disk. See issue #27.
    """
    priority = {name.lower(): i for i, name in enumerate(RESUME_CANDIDATES)}
    matches = [
        p for p in cwd.iterdir()
        if p.is_file() and p.name.lower() in priority
    ]
    if not matches:
        return None
    matches.sort(key=lambda p: priority[p.name.lower()])
    return matches[0]


ACCEPTED_RESUME_EXTENSIONS = frozenset({".md", ".markdown", ".pdf"})


def validate_resume_path(cwd: Path, filename: str) -> tuple[Optional[Path], Optional[str]]:
    """
    Validate a student-provided resume filename and return (abs_path, None) if
    acceptable, or (None, error_message) otherwise.

    Used as the fallback when `discover_resume` returns None (e.g., the
    student's file is named `Lebenslauf.md`, `履歴書.md`, `my_resume_v3.md`,
    or anything else not in RESUME_CANDIDATES). The SKILL.md orchestrator
    asks the student "what's the filename?" via the cross-host question tool
    and feeds the response through this validator.

    Accepts:
    - A relative path (resolved against `cwd`).
    - An absolute path (used as-is).
    - Any Unicode filename including CJK characters, spaces, and hyphens.
    - Extensions .md / .markdown / .pdf (case-insensitive).

    Rejects:
    - Files that don't exist.
    - Files with unsupported extensions (.docx, .txt, .rtf, etc).
    - Directories (even if the name looks like a resume).
    - Paths the current process can't read.
    """
    if not filename or not filename.strip():
        return None, "filename is empty"

    filename = filename.strip()
    # Accept both relative-to-cwd and absolute paths.
    candidate = Path(filename) if Path(filename).is_absolute() else (cwd / filename)

    # Resolve symlinks + ".." segments; works even if the file doesn't exist yet.
    # (Path.resolve(strict=False) was added in 3.6; we target 3.10+.)
    try:
        candidate = candidate.resolve()
    except (OSError, RuntimeError):
        return None, f"could not resolve path: {filename}"

    if not candidate.exists():
        return None, f"file does not exist: {candidate}"
    if not candidate.is_file():
        return None, f"not a regular file (directory or special): {candidate}"

    ext = candidate.suffix.lower()
    if ext not in ACCEPTED_RESUME_EXTENSIONS:
        accepted = ", ".join(sorted(ACCEPTED_RESUME_EXTENSIONS))
        return None, (
            f"unsupported extension {ext or '(none)'}: {candidate}. "
            f"Accepted: {accepted}"
        )

    try:
        # Probe readability without loading the whole file.
        with candidate.open("rb") as f:
            f.read(1)
    except OSError as exc:
        return None, f"file is not readable: {candidate} ({exc})"

    return candidate, None


# ---------------------------------------------------------------------------
# 2.5 CleanupAction: record type for the stray-file scans
# ---------------------------------------------------------------------------


@dataclass
class CleanupAction:
    path: Path  # absolute path to the rogue file (pre-action)
    action: str  # "moved" | "deleted" | "skipped"
    reason: str  # human-readable explanation
    destination: Optional[Path] = None  # only set when action == "moved"


# ---------------------------------------------------------------------------
# 2.6 cleanup_stray_prompts: defense-in-depth for prompt-staging PII leaks
# ---------------------------------------------------------------------------
#
# Background: real-run testing of #43 surfaced that some agents (Claude Code's
# Bash tool was the observed offender on macOS) improvise around SKILL.md's
# prompt-build pattern. Instead of capturing the rendered prompt in a shell
# variable (which can hit argv-length caps for the folder-miner prompt at
# 100KB+), they write it to `/tmp/<kind>-prompt.txt` and pass the path. That
# leaks the student's resume + JD + project content as plaintext PII into a
# directory that's world-readable on macOS (mode 0755) until reboot.
#
# Belt-and-suspenders fix:
#   - Suspenders: SKILL.md now prescribes `$RUN_DIR/prompts/<kind>.txt`
#     for prompt staging (gitignored, run-scoped, wiped each run).
#   - Belt: this scan runs at end of Phase 8 to catch and delete any
#     /tmp/<kind>-prompt.{txt,md} file the agent improvised anyway.
#
# Anti-footgun rules:
#   - Top-level only (never recursive). /tmp has many other tools' files;
#     we only look at the immediate top.
#   - mtime gate: only files newer than the run's start timestamp are
#     candidates. Pre-existing /tmp files (other tools' debug output) are
#     never touched.
#   - Name match: basename must equal `<kind>-prompt.<ext>` or
#     `<kind>_prompt.<ext>` for a kind in PROMPT_KINDS, with .txt / .md
#     suffix. Generic /tmp files like `bash_history` or `screenshot.png`
#     are never touched.
#   - Action: delete unconditionally. These are transient sub-agent
#     intermediates; there's no recovery value, and they contain PII we
#     want gone. (If you ever decide you want them moved-and-logged for
#     debugging, file a new issue and we can flip the action.)


def _registered_prompt_kinds() -> tuple[str, ...]:
    """Returns the registered prompt kind names. Kept as a function so the
    cleanup pattern set updates automatically as new kinds are added to
    `scripts/prompts.py` — there's no second list to keep in sync."""
    return tuple(_PROMPT_KINDS)


def cleanup_stray_prompts(
    since_timestamp: float,
    scan_dir: Path = Path("/tmp"),
) -> list[CleanupAction]:
    """Scan `scan_dir` (default `/tmp`) top-level for prompt-staging files
    a sub-agent improvised, deleting any whose mtime is newer than
    `since_timestamp` and whose basename matches `<kind>-prompt.{txt,md}`
    or `<kind>_prompt.{txt,md}` for a kind in `_PROMPT_KINDS`.

    Returns a list of CleanupAction records. Never raises on a single
    bad file — records a `skipped` action and moves on. If `scan_dir`
    doesn't exist or isn't a directory, returns an empty list (some
    constrained runtimes don't have /tmp).
    """
    actions: list[CleanupAction] = []
    if not scan_dir.exists() or not scan_dir.is_dir():
        return actions

    kinds = _registered_prompt_kinds()
    # Build the matched-name set: for each kind name, both the canonical
    # hyphen-separator form (`folder-miner-prompt`) and the all-underscore
    # form (`folder_miner_prompt`) are accepted, with both `-prompt` and
    # `_prompt` connector separators. Agents improvise across all these
    # combinations in real runs (Eduardo's logs showed both variants from
    # different sessions).
    expected_stems: set[str] = set()
    for kind in kinds:
        kind_lower = kind.lower()
        kind_underscore = kind_lower.replace("-", "_")
        for stem_prefix in {kind_lower, kind_underscore}:
            for sep in ("-", "_"):
                expected_stems.add(f"{stem_prefix}{sep}prompt")

    try:
        entries = list(scan_dir.iterdir())
    except OSError:
        return actions

    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in (".txt", ".md"):
                continue
            if entry.stem.lower() not in expected_stems:
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime <= since_timestamp:
                continue
        except OSError:
            continue

        try:
            entry.unlink()
            actions.append(
                CleanupAction(
                    path=entry,
                    action="deleted",
                    reason=(
                        "transient sub-agent prompt-staging file in /tmp; "
                        "SKILL.md prescribes $RUN_DIR/prompts/ for staging"
                    ),
                )
            )
        except OSError as exc:
            actions.append(
                CleanupAction(
                    path=entry,
                    action="skipped",
                    reason=f"delete failed: {exc}",
                )
            )

    return actions


# ---------------------------------------------------------------------------
# 3. read file with encoding detection (chardet fallback to utf-8-sig / utf-8)
# ---------------------------------------------------------------------------


def _read_text_with_encoding_detection(path: Path) -> str:
    """
    Read `path` as text, detecting encoding when UTF-8 decode fails, AND
    normalizing line endings to LF (``\\n``) on the way out.

    Handles two footguns:

    1. The Windows-Notepad-UTF-16-BOM footgun from the eng review — try
       UTF-8 first (fast path), then chardet, then fail loudly.
    2. Cross-platform line endings — Windows files commonly carry
       ``\\r\\n`` endings. When such content travels through a text-mode
       subprocess pipe, Python's universal-newline behavior applies twice
       (once on stdout-write because text mode converts ``\\n`` to
       ``\\r\\n`` on Windows, so the file's existing ``\\r\\n`` becomes
       ``\\r\\r\\n``; once on the receiving side, where ``\\r\\r\\n``
       decodes as two newlines), doubling every line break. Normalizing
       to LF here matches what Python's text-mode read does for
       ``open(path, 'r')`` and gives every downstream consumer
       deterministic line endings regardless of host.
    """
    raw = path.read_bytes()

    # Fast path: UTF-8 (handles UTF-8, UTF-8-BOM via utf-8-sig retry).
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            # Chardet path.
            detection = chardet.detect(raw)
            encoding = detection.get("encoding") or "latin-1"
            confidence = detection.get("confidence") or 0.0
            if confidence < 0.5:
                raise UnicodeDecodeError(
                    encoding, raw, 0, 1,
                    f"Could not reliably detect encoding of {path} "
                    f"(best guess: {encoding} with {confidence:.0%} confidence). "
                    f"Please resave the file as UTF-8."
                )
            text = raw.decode(encoding)

    # Normalize line endings — match Python text-mode read semantics so
    # every consumer sees ``\n`` regardless of the file's native line
    # endings. Order: ``\r\n`` first (Windows), then bare ``\r`` (legacy
    # Classic Mac).
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def read_resume(path: Path) -> str:
    """
    Read a resume file as text. Handles markdown (with encoding detection)
    and PDF (via pdfminer.six text extraction).

    Raises a clear error if the PDF appears to be image-only (scanned resume
    with no extractable text). In that case the student needs to either OCR
    it themselves or retype the content into a resume.md.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_resume_pdf(path)
    return _read_text_with_encoding_detection(path)


def _read_resume_pdf(path: Path) -> str:
    """
    Extract selectable text from a PDF resume. pdfminer returns text in
    approximate reading order which is usually good enough for the tailor
    sub-agent to restructure into the markdown schema.
    """
    try:
        from pdfminer.high_level import extract_text
    except ImportError as exc:
        raise RuntimeError(
            "pdfminer.six is required to read PDF resumes but is not installed. "
            "Run install.sh inside the skill directory to set up the venv."
        ) from exc

    try:
        text = extract_text(str(path)) or ""
    except Exception as exc:
        raise RuntimeError(
            f"Failed to extract text from {path}: {exc}. "
            f"The PDF may be corrupted or encrypted."
        ) from exc

    # Heuristic: fewer than 50 non-whitespace characters means the PDF is
    # almost certainly image-based (a scanned resume). pdfminer cannot do OCR.
    stripped = "".join(text.split())
    if len(stripped) < 50:
        raise RuntimeError(
            f"{path} appears to be an image-based (scanned) PDF — only "
            f"{len(stripped)} characters of selectable text were extracted. "
            f"resumasher cannot OCR scanned PDFs. Options: "
            f"(1) export a text-based PDF from your source document, "
            f"(2) run OCR yourself (e.g., `ocrmypdf`) and retry, or "
            f"(3) create a resume.md in the same folder and resumasher will use that instead."
        )

    return text


# ---------------------------------------------------------------------------
# 4. folder_state_hash: sha256 of sorted (relpath, mtime_ns, size) tuples
# ---------------------------------------------------------------------------

DEFAULT_IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".resumasher",
    ".pytest_cache",
    ".ipynb_checkpoints",
    "applications",
    ".DS_Store",
    # Critical: when resumasher is installed project-scope at
    # <project>/.claude/skills/resumasher/ (or .codex, .gemini, .opencode),
    # the folder miner would otherwise walk its own source tree +
    # GOLDEN_FIXTURES and present them to the sub-agents as the student's
    # evidence. These dirs hold AI CLI skills/agents/settings — never
    # resume evidence.
    ".claude",
    ".codex",
    ".gemini",
    ".opencode",
    ".agents",
}


def _iter_files(cwd: Path, ignore_dirs: Iterable[str]) -> Iterable[Path]:
    ignore = set(ignore_dirs)
    for root, dirs, files in os.walk(cwd):
        # Prune ignored directories in-place so os.walk doesn't descend into them.
        dirs[:] = [d for d in dirs if d not in ignore]
        for name in files:
            if name in ignore:
                continue
            yield Path(root) / name


def folder_state_hash(cwd: Path, ignore_dirs: Optional[Iterable[str]] = None) -> str:
    """
    Hash the folder by (relpath, mtime_ns, size) tuples. Any touch, move, or
    resize invalidates the cache.

    Ignored: .git, .venv, node_modules, __pycache__, .resumasher, applications
    and any entry explicitly listed in ignore_dirs.
    """
    ignore = set(ignore_dirs or DEFAULT_IGNORE_DIRS)
    triples: list[tuple[str, int, int]] = []
    for p in _iter_files(cwd, ignore):
        try:
            st = p.stat()
        except OSError:
            continue
        rel = p.relative_to(cwd).as_posix()
        triples.append((rel, st.st_mtime_ns, st.st_size))
    triples.sort()
    h = hashlib.sha256()
    for rel, mtime, size in triples:
        h.update(f"{rel}|{mtime}|{size}\n".encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 5. mine_folder_context: build the context block handed to the LLM miner
# ---------------------------------------------------------------------------

TEXT_EXTENSIONS = {".md", ".markdown", ".py", ".sql", ".r", ".rmd", ".txt", ".rst"}
PDF_EXTENSIONS = {".pdf"}
NOTEBOOK_EXTENSIONS = {".ipynb"}
SKIP_EXTENSIONS = {
    ".csv", ".parquet", ".pkl", ".pt", ".h5", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".mp4", ".mov", ".avi",
    ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar",
    ".xlsx", ".xls", ".doc", ".docx",
}

MAX_FILE_CHARS = 50_000   # 50KB cap, matches design doc
MAX_CONTEXT_CHARS = 80_000  # hard ceiling on total miner context


def _classify(path: Path) -> str:
    """Return one of: 'text', 'pdf', 'notebook', 'readme', 'skip'."""
    # README files are always included regardless of extension.
    if path.name.lower().startswith("readme"):
        return "readme"
    suffix = path.suffix.lower()
    if suffix in NOTEBOOK_EXTENSIONS:
        return "notebook"
    if suffix in PDF_EXTENSIONS:
        return "pdf"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    if suffix in SKIP_EXTENSIONS:
        return "skip"
    return "skip"


def _extract_pdf_text(path: Path, max_chars: int = MAX_FILE_CHARS) -> str:
    try:
        from pdfminer.high_level import extract_text
    except ImportError:
        return f"[PDF: pdfminer.six not installed, cannot extract {path.name}]"
    try:
        text = extract_text(str(path)) or ""
    except Exception as e:
        return f"[PDF extract failed for {path.name}: {e}]"
    if len(text) > max_chars:
        return text[:max_chars] + f"\n[...truncated at {max_chars} chars]"
    return text


def _extract_notebook_text(path: Path, max_chars: int = MAX_FILE_CHARS) -> str:
    """Try nbconvert first; fall back to a lightweight JSON parser."""
    try:
        from nbconvert import MarkdownExporter
        exporter = MarkdownExporter()
        body, _ = exporter.from_filename(str(path))
        if len(body) > max_chars:
            return body[:max_chars] + f"\n[...truncated at {max_chars} chars]"
        return body
    except Exception:
        # Fallback: pull `source` fields from code + markdown cells only.
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            return f"[notebook parse failed for {path.name}: {e}]"
        parts: list[str] = []
        for cell in data.get("cells", []):
            cell_type = cell.get("cell_type")
            if cell_type not in {"code", "markdown"}:
                continue
            source = cell.get("source", "")
            if isinstance(source, list):
                source = "".join(source)
            if cell_type == "code":
                parts.append("```python\n" + source.rstrip() + "\n```")
            else:
                parts.append(source.rstrip())
        body = "\n\n".join(parts)
        if len(body) > max_chars:
            return body[:max_chars] + f"\n[...truncated at {max_chars} chars]"
        return body


def _extract_plain_text(path: Path, max_chars: int = MAX_FILE_CHARS) -> str:
    try:
        text = _read_text_with_encoding_detection(path)
    except Exception as e:
        return f"[read failed for {path.name}: {e}]"
    if len(text) > max_chars:
        return text[:max_chars] + f"\n[...truncated at {max_chars} chars]"
    return text


def mine_folder_context(
    cwd: Path,
    ignore_dirs: Optional[Iterable[str]] = None,
    max_context_chars: int = MAX_CONTEXT_CHARS,
) -> str:
    """
    Walk `cwd`, extract text from allowed files, return a single prose context
    block the folder-miner sub-agent will consume.

    Layout:
        === FILE: <relpath> (<size> bytes) ===
        <content>

    Files are ordered by relpath for determinism. If total context exceeds
    max_context_chars, later files are replaced with a "[skipped: N files,
    budget exhausted]" summary so the miner at least knows they exist.
    """
    ignore = set(ignore_dirs or DEFAULT_IGNORE_DIRS)
    entries: list[tuple[str, int, str]] = []  # (relpath, size, content)
    deferred: list[str] = []

    total = 0
    # Collect all first so we can order deterministically.
    files = sorted(_iter_files(cwd, ignore), key=lambda p: p.relative_to(cwd).as_posix())

    for path in files:
        rel = path.relative_to(cwd).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        kind = _classify(path)
        if kind == "skip":
            continue

        if total > max_context_chars:
            deferred.append(rel)
            continue

        if kind == "pdf":
            content = _extract_pdf_text(path)
        elif kind == "notebook":
            content = _extract_notebook_text(path)
        else:  # text, readme
            content = _extract_plain_text(path)

        entries.append((rel, size, content))
        total += len(content) + 120  # overhead estimate for header

    chunks: list[str] = []
    for rel, size, content in entries:
        chunks.append(f"=== FILE: {rel} ({size} bytes) ===\n{content.rstrip()}")

    if deferred:
        chunks.append(
            f"=== DEFERRED: {len(deferred)} files skipped due to context budget ===\n"
            + "\n".join(deferred[:50])
        )

    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# 6. extract company / role from the job-extractor's prose
# ---------------------------------------------------------------------------

# Each pattern accepts the plain `KEY: value` form prescribed by the
# job-extractor prompt AND markdown-bold variants weaker models emit
# despite the "on a line by itself" instruction:
#   - **KEY:** value             (bold around key+colon)
#   - **KEY**: value             (bold around just the key)
#   - KEY: **value**             (bold around just the value)
#   - **KEY:** **value**         (bold around both, separately)
# Observed under qwen3.6-35b on OpenCode (run ses_236d) — the model
# produced `**ROLE:** Data Analyst` instead of `ROLE: Data Analyst`,
# leaving role.txt empty.
#
# The trick is `[\s*]*` (character class of whitespace OR `*`) before
# and after each meaningful token: it absorbs any mix of `*` markers
# and spaces in any order, regardless of which side of the colon the
# bold delimiters sit on. Earlier attempts with separate `\*{0,2}`
# groups misparsed `**KEY:** **value**` because the closing `**` of
# the key-bold is structurally indistinguishable from the opening
# `**` of the value-bold.
#
# Both stay anchored to line starts to avoid hijacking inline `ROLE:`
# mentions in body prose. `(.+?)` lazy + trailing `[\s*]*$` greedy means
# `(.+?)` captures the value with trailing `*` and whitespace stripped.
_COMPANY_RE = re.compile(r"^[\s*]*COMPANY[\s*]*:[\s*]*(.+?)[\s*]*$", re.MULTILINE | re.IGNORECASE)
_ROLE_RE = re.compile(r"^[\s*]*ROLE[\s*]*:[\s*]*(.+?)[\s*]*$", re.MULTILINE | re.IGNORECASE)
_FAILURE_RE = re.compile(r"^[\s*]*FAILURE[\s*]*:[\s*]*.+", re.IGNORECASE)


def extract_company(prose: str) -> Optional[str]:
    """Return the COMPANY: value, stripped. 'UNKNOWN' -> None."""
    match = _COMPANY_RE.search(prose)
    if not match:
        return None
    value = match.group(1).strip()
    if value.upper() == "UNKNOWN" or not value:
        return None
    return value


def extract_role(prose: str) -> Optional[str]:
    """Return the ROLE: value, stripped. Blank / UNKNOWN / missing -> None."""
    match = _ROLE_RE.search(prose)
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value.upper() == "UNKNOWN":
        return None
    return value


def is_failure_sentinel(prose: str) -> bool:
    """True if the first non-blank line starts with 'FAILURE:'."""
    for line in prose.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return bool(_FAILURE_RE.match(stripped))
    return False


# The job-extractor emits pipe-separated term lists. Commas are NOT the
# separator: real requirement strings contain them ("Analyst, Commercial
# Data", "R, Python"), so a comma split would shred them.
_HARD_REQ_RE = re.compile(r"^[\s*]*HARD_REQUIREMENTS[\s*]*:[\s*]*(.+?)[\s*]*$", re.MULTILINE | re.IGNORECASE)
_PREFERRED_RE = re.compile(r"^[\s*]*PREFERRED[\s*]*:[\s*]*(.+?)[\s*]*$", re.MULTILINE | re.IGNORECASE)
_TITLE_VARIANTS_RE = re.compile(r"^[\s*]*TITLE_VARIANTS[\s*]*:[\s*]*(.+?)[\s*]*$", re.MULTILINE | re.IGNORECASE)

# Sentinels the LLM emits for "this list is empty". Compared case-folded.
_EMPTY_LIST_VALUES = frozenset({"none", "n/a", "na", "unknown", "-", ""})


def _split_terms(raw: Optional[str]) -> list[str]:
    """Split a pipe-separated term list into clean terms, order preserved.

    Drops empties, strips markdown bullet/bold residue, de-duplicates
    case-insensitively while keeping the first-seen surface form (which is
    the JD's own spelling — the whole point of the extraction).
    """
    if raw is None:
        return []
    if raw.strip().lower() in _EMPTY_LIST_VALUES:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for chunk in raw.split("|"):
        term = chunk.strip().strip("*_`").strip()
        term = term.lstrip("-• ").strip()
        if not term or term.lower() in _EMPTY_LIST_VALUES:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def extract_hard_requirements(prose: str) -> list[str]:
    """Terms from the HARD_REQUIREMENTS line, in the JD's surface form."""
    match = _HARD_REQ_RE.search(prose)
    return _split_terms(match.group(1) if match else None)


def extract_preferred(prose: str) -> list[str]:
    """Terms from the PREFERRED line, in the JD's surface form."""
    match = _PREFERRED_RE.search(prose)
    return _split_terms(match.group(1) if match else None)


def extract_title_variants(prose: str) -> list[str]:
    """Role-title spellings the JD uses."""
    match = _TITLE_VARIANTS_RE.search(prose)
    return _split_terms(match.group(1) if match else None)


def render_jd_keywords(
    hard: list[str], preferred: list[str], titles: list[str]
) -> str:
    """Render the extracted terms as the `jd_keywords` prompt variable.

    Plain labelled lines rather than JSON: the sub-agent reads this as
    guidance, and a bulleted block is easier for a model to follow than a
    nested object. Empty sections say so explicitly instead of vanishing,
    so a model that sees "PREFERRED: (none listed)" doesn't go hunting for
    a section that was silently omitted.
    """
    def _fmt(label: str, terms: list[str]) -> str:
        if not terms:
            return f"{label}: (none listed)"
        return f"{label}:\n" + "\n".join(f"  - {t}" for t in terms)

    return "\n".join([
        _fmt("HARD REQUIREMENTS (use these exact spellings when the "
             "candidate's evidence supports the term)", hard),
        "",
        _fmt("PREFERRED", preferred),
        "",
        _fmt("ROLE TITLE AS THE JD WRITES IT", titles),
    ])


# ---------------------------------------------------------------------------
# 6.5 keyword_coverage: which JD terms made it into the tailored resume
# ---------------------------------------------------------------------------
#
# Deterministic, no LLM. The student gets an honest list of what the
# screening layer will and won't find. Missing terms are NOT automatically
# a defect: a term is missing either because the candidate genuinely lacks
# that experience (correct, and the anchoring rule forbids inventing it) or
# because the tailor phrased it differently (fixable, and worth surfacing).
# We report; the student decides.


def _normalize_for_match(text: str) -> str:
    """Casefold and collapse punctuation/whitespace so surface variants match.

    'Power BI' / 'power  bi' / 'Power-BI' all normalize to 'power bi'.
    Deliberately does NOT strip the internal structure of things like
    'A/B testing' beyond turning the slash into a space, because that is
    how a term written 'A/B' vs 'A B' should still land.
    """
    text = text.casefold()
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _acronym_forms(term: str) -> list[str]:
    """Return the match candidates for a term.

    For 'Natural Language Processing (NLP)' this yields the full string,
    the expansion alone, and the acronym alone — a resume matching ANY of
    the three has satisfied the requirement, since the parenthetical is
    the JD's own way of saying "these are the same thing."
    """
    forms = [term]
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", term)
    if m:
        outer, inner = m.group(1).strip(), m.group(2).strip()
        if outer:
            forms.append(outer)
        if inner:
            forms.append(inner)
    return forms


def keyword_coverage(terms: list[str], document: str) -> dict:
    """Report which `terms` appear in `document`.

    Substring match on the normalized forms. Substring rather than
    token-boundary matching because requirement terms are frequently
    multi-word phrases embedded mid-bullet, and a boundary regex per term
    would cost more than it buys at this list size (<=30 terms).
    """
    haystack = _normalize_for_match(document)
    matched: list[str] = []
    missing: list[str] = []
    for term in terms:
        forms = [_normalize_for_match(f) for f in _acronym_forms(term)]
        if any(f and f in haystack for f in forms):
            matched.append(term)
        else:
            missing.append(term)
    total = len(terms)
    return {
        "total": total,
        "matched": matched,
        "missing": missing,
        "matched_count": len(matched),
        "missing_count": len(missing),
        # Percent is reported for the student's intuition only. It is NOT
        # the ATS score — real systems weight terms unequally and read
        # context. Treat it as "how much of the vocabulary is present."
        "percent": round(100.0 * len(matched) / total, 1) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# 6.6 lint_output: catch machine-written tells and unresolved markup
# ---------------------------------------------------------------------------
#
# Deterministic, no LLM. Two jobs:
#
#   1. Unresolved authoring markup that must never reach a recruiter.
#      `[INSERT ...]` is obvious. `<!--SOFT: ...-->` is the sneaky one:
#      HTML comments are invisible in a markdown preview but paste into
#      Word as literal visible text, so a student who skips the edit step
#      ships a resume with the annotation printed in it.
#
#   2. Phrases and punctuation that recruiters report as machine-written
#      tells. The prompts already forbid these (prompts.py
#      HUMAN_VOICE_RULES); this is the belt to that suspenders, because a
#      weaker model will emit them regardless of instruction.
#
# Findings are WARNINGS, never errors. The student decides — some of these
# are legitimately the right word in context, and a linter that blocks on
# style would be worse than one that informs.

# Keep in sync with HUMAN_VOICE_RULES in scripts/prompts.py.
AI_TELL_PHRASES: tuple[str, ...] = (
    # Cover-letter openings that mark a letter as templated
    "i am writing to express",
    "i am writing to apply",
    "i am excited to apply",
    "i was thrilled to see",
    "please accept this letter",
    "i am reaching out regarding",
    "i would welcome the opportunity to discuss",
    # Empty self-description
    "results-driven", "results-oriented", "detail-oriented", "self-starter",
    "go-getter", "team player", "hardworking", "highly motivated",
    "proven track record", "track record of success", "thought leader",
    "hit the ground running", "wear many hats", "dynamic professional",
    # Machine-written vocabulary
    "delve", "seamless", "tapestry", "cutting-edge", "state-of-the-art",
    "best-in-class", "world-class", "game-changer", "synergy", "synergize",
    "spearheaded", "pivotal", "myriad", "plethora", "testament to",
    # Constructions
    "not just", "more than just", "in conclusion",
    "moreover,", "furthermore,",
)

_EM_DASH_RE = re.compile(r"[—]")
# En dash flagged only when NOT between two date-ish tokens, since
# "Mar 2022 – Aug 2024" is the prescribed date format.
_EN_DASH_PROSE_RE = re.compile(
    r"(?<![0-9A-Za-z]{3}\s)(?<![0-9]\s)–(?!\s*(?:present|current|\d))",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+\s+")


def _strip_markdown_noise(text: str) -> str:
    """Remove code blocks and HTML comments before prose-level checks."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    return text


def lint_output(document: str, *, kind: str = "resume") -> list[dict]:
    """Return a list of warning dicts for `document`.

    `kind` is "resume" or "cover-letter"; it only affects which structural
    checks run (sentence-rhythm analysis is meaningless on bullet
    fragments, so it is cover-letter only).
    """
    warnings: list[dict] = []

    for match in re.finditer(r"\[INSERT[^\]]*\]", document, re.IGNORECASE):
        warnings.append({
            "code": "UNRESOLVED_PLACEHOLDER",
            "text": match.group(0),
            "hint": "Fill in the real number, or delete this version and "
                    "keep the SOFT alternate on the same line.",
        })

    if "<!--SOFT" in document:
        count = document.count("<!--SOFT")
        warnings.append({
            "code": "SOFT_COMMENT_PRESENT",
            "text": f"{count} <!--SOFT: ...--> comment(s)",
            "hint": "These are invisible in a markdown preview but paste "
                    "into Word as visible text. Resolve each bullet, then "
                    "delete the comment.",
        })

    prose = _strip_markdown_noise(document)

    em_dashes = _EM_DASH_RE.findall(prose)
    if em_dashes:
        warnings.append({
            "code": "EM_DASH",
            "text": f"{len(em_dashes)} em dash(es)",
            "hint": "Replace with a period, comma, or colon.",
        })

    en_dashes = _EN_DASH_PROSE_RE.findall(prose)
    if en_dashes:
        warnings.append({
            "code": "EN_DASH_IN_PROSE",
            "text": f"{len(en_dashes)} en dash(es) outside a date range",
            "hint": "Date ranges keep the en dash. Prose should not.",
        })

    lowered = prose.casefold()
    for phrase in AI_TELL_PHRASES:
        if phrase in lowered:
            warnings.append({
                "code": "AI_TELL_PHRASE",
                "text": phrase,
                "hint": "Recruiters flag this as machine-written. Replace "
                        "it with the specific evidence behind the claim.",
            })

    if kind == "cover-letter":
        paragraphs = [
            p.strip() for p in re.split(r"\n\s*\n", prose)
            if len(p.strip().split()) >= 25
        ]
        for para in paragraphs:
            sentences = [
                s for s in _SENTENCE_SPLIT_RE.split(para.strip()) if s.strip()
            ]
            if len(sentences) < 3:
                continue
            lengths = [len(s.split()) for s in sentences]
            # HUMAN_VOICE_RULES asks for at least one sentence under 8
            # words. The linter is deliberately a couple of words more
            # lenient than the prompt: the prompt sets the target, this
            # catches paragraphs that missed it outright rather than
            # nagging about a 9-word sentence.
            if min(lengths) >= 10:
                warnings.append({
                    "code": "UNIFORM_SENTENCE_LENGTH",
                    "text": f"paragraph with sentence lengths {lengths}",
                    "hint": "No short sentence in this paragraph. Uniform "
                            "sentence length is a strong machine-written "
                            "signal. Break one idea into a short sentence.",
                })

        if len(paragraphs) >= 3:
            counts = [len(p.split()) for p in paragraphs]
            spread = max(counts) - min(counts)
            if spread <= 15:
                warnings.append({
                    "code": "UNIFORM_PARAGRAPH_LENGTH",
                    "text": f"paragraph word counts {counts}",
                    "hint": "Near-identical paragraph lengths are the "
                            "template shape recruiters recognize. Make one "
                            "paragraph noticeably shorter.",
                })

    return warnings


# ---------------------------------------------------------------------------
# 7. company_slug: safe directory name
# ---------------------------------------------------------------------------


def company_slug(name: str) -> str:
    """Turn 'Deloitte Consulting LLC' into 'deloitte-consulting'.

    Unicode-preserving: 'Müller GmbH' → 'müller' (keeps the umlaut, drops
    the legal suffix). Relies on Python 3's default Unicode \\w for letters.
    """
    if not name or not name.strip():
        return "unknown"
    s = name.strip().lower()
    # Drop common legal-entity suffixes people don't want in directory names.
    s = re.sub(r"\b(gmbh|s\.?a\.?|ag|llc|inc|ltd|corp|plc|co|company)\b\.?", "", s)
    # Collapse any run of non-word chars (including whitespace, punctuation,
    # ampersand, but NOT accented letters) into a single hyphen.
    s = re.sub(r"[^\w]+", "-", s, flags=re.UNICODE)
    s = s.strip("-_")
    return s or "unknown"


# ---------------------------------------------------------------------------
# 8. append_history
# ---------------------------------------------------------------------------


def append_history(cwd: Path, record: dict) -> Path:
    """Append a JSON line to .resumasher/history.jsonl. Creates dir if needed."""
    target = cwd / ".resumasher" / "history.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target


# ---------------------------------------------------------------------------
# 9. first_run_setup: config.json + gitignore + GDPR note
# ---------------------------------------------------------------------------


GDPR_NOTE = (
    "resumasher stores your contact info and application history LOCALLY in\n"
    ".resumasher/ inside this folder. If this folder is a git repo, we will\n"
    "add .resumasher/ to your .gitignore automatically.\n"
    "\n"
    "Nothing is uploaded. Your resume, job descriptions, and generated\n"
    "application files never leave this machine. The only network calls\n"
    "resumasher makes are the ones you ask for: fetching a job posting URL,\n"
    "reading your public GitHub profile, and the company web search."
)


def first_run_needed(cwd: Path) -> bool:
    return not (cwd / ".resumasher" / "config.json").exists()


def write_config(cwd: Path, config: dict) -> Path:
    target = cwd / ".resumasher" / "config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def read_config(cwd: Path) -> Optional[dict]:
    target = cwd / ".resumasher" / "config.json"
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def ensure_gitignore(cwd: Path) -> Optional[Path]:
    """
    If `cwd` is inside a git repo, append `.resumasher/` to .gitignore if the
    entry isn't already there. Return the path written, or None if no git repo.
    """
    # Look upward for a .git dir to decide if we're in a repo.
    for parent in [cwd, *cwd.parents]:
        if (parent / ".git").exists():
            gitignore = cwd / ".gitignore"
            existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
            if ".resumasher/" in existing or ".resumasher" in existing.split():
                return gitignore
            new_content = existing
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            new_content += ".resumasher/\n"
            gitignore.write_text(new_content, encoding="utf-8")
            return gitignore
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> int:
    parser = argparse.ArgumentParser(prog="scripts.orchestration")
    sub = parser.add_subparsers(dest="command", required=True)

    # parse-job-source as a single command emitted JSON-on-stdout, which
    # broke when shells (zsh, dash, bash with xpg_echo) interpret backslash
    # escapes — the JSON's `\n` got rewritten to a real newline before
    # downstream parsing. See issue #44 for the analysis. Replaced by two
    # narrow single-purpose commands; orchestrators capture the mode word
    # in a shell variable and pipe content directly through format-jd
    # without ever round-tripping JSON through a shell string.
    p = sub.add_parser(
        "parse-job-mode",
        help=(
            "Resolve <arg> and emit one of: file, url, literal. "
            "Single word on stdout, safe to capture in a shell variable. "
            "Pair with parse-job-content for the corresponding payload."
        ),
    )
    p.add_argument("arg")
    p.add_argument("--cwd", default=".")

    p = sub.add_parser(
        "parse-job-content",
        help=(
            "Resolve <arg> and emit the corresponding payload to stdout: "
            "for file mode, the file's text; for url mode, the URL; for "
            "literal mode, the literal text. Raw bytes — no JSON wrap, "
            "safe to pipe directly through format-jd."
        ),
    )
    p.add_argument("arg")
    p.add_argument("--cwd", default=".")

    p = sub.add_parser("format-jd")
    p.add_argument("--mode", required=True, choices=["file", "url", "literal"])
    p.add_argument("--url", default=None, help="Source URL (for mode=url; prepended as a header line)")
    p.add_argument(
        "--content-file",
        default="-",
        help="Path to a file containing the JD text, or '-' for stdin (default).",
    )

    p = sub.add_parser("discover-resume")
    p.add_argument("cwd", nargs="?", default=".")

    p = sub.add_parser("validate-resume-path")
    p.add_argument("cwd")
    p.add_argument("filename")

    p = sub.add_parser("folder-state-hash")
    p.add_argument("cwd", nargs="?", default=".")

    p = sub.add_parser("mine-context")
    p.add_argument("cwd", nargs="?", default=".")
    p.add_argument(
        "--github-username",
        default=None,
        help="Also mine this GitHub profile and append its prose to the context",
    )

    p = sub.add_parser("github-mine")
    p.add_argument("username")
    p.add_argument("--cwd", default=".")
    p.add_argument("--cap", type=int, default=15)
    p.add_argument("--no-cache", action="store_true")

    p = sub.add_parser("read-resume")
    p.add_argument("path")

    p = sub.add_parser("extract-company")
    p = sub.add_parser("extract-role")

    p = sub.add_parser(
        "extract-job-fields",
        help=(
            "Read job-extractor text on stdin, extract company / role / "
            "screening terms, and write each to its own file under "
            "--output-dir. Per-field files rather than a shell-sourced env "
            "file, which breaks when company / role contain spaces (#50)."
        ),
    )
    p.add_argument(
        "--output-dir",
        required=True,
        help=(
            "Directory to write per-field files into. Created if missing. "
            "Files written: company.txt, role.txt, hard-requirements.txt, "
            "preferred.txt, title-variants.txt, keywords.txt"
        ),
    )

    p = sub.add_parser(
        "keyword-coverage",
        help=(
            "Deterministic (no LLM): report which JD screening terms appear "
            "in the tailored resume. Reads the term files written by "
            "extract-job-fields. Always exits 0 — missing terms are "
            "information for the student, not a build failure."
        ),
    )
    p.add_argument("--job-dir", required=True, help="Directory holding hard-requirements.txt / preferred.txt")
    p.add_argument("--resume", required=True, help="Path to the tailored resume markdown")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text")

    p = sub.add_parser(
        "lint-output",
        help=(
            "Deterministic (no LLM): flag unresolved [INSERT ...] "
            "placeholders, leftover <!--SOFT: ...--> comments, em dashes, "
            "and phrasing recruiters report as machine-written. Warnings "
            "only; always exits 0."
        ),
    )
    p.add_argument("--input", required=True, help="Path to the markdown file to lint")
    p.add_argument(
        "--kind",
        choices=["resume", "cover-letter"],
        default="resume",
        help="Enables the sentence/paragraph rhythm checks for cover letters",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text")

    p = sub.add_parser("is-failure")

    p = sub.add_parser("append-history")
    p.add_argument("cwd")
    p.add_argument("json_line")

    p = sub.add_parser(
        "cleanup-stray-prompts",
        help=(
            "Defense-in-depth: scan /tmp for prompt-staging files a "
            "sub-agent improvised (issue #45). Files newer than "
            "--since-timestamp whose basenames match <kind>-prompt.{txt,md} "
            "or <kind>_prompt.{txt,md} for a registered prompt kind are "
            "deleted unconditionally — they're transient sub-agent "
            "intermediates containing student PII (resume, JD, project "
            "content). Emits a JSON summary on stdout. Always exits 0 — "
            "cleanup failures are logged but never block the orchestrator."
        ),
    )
    p.add_argument(
        "--since-timestamp",
        type=float,
        required=True,
        help="Epoch seconds; only files with mtime newer than this are candidates",
    )
    p.add_argument(
        "--scan-dir",
        default="/tmp",
        help=(
            "Directory to scan, top-level only (default: /tmp). Mainly "
            "useful for tests; production callers should leave this at "
            "the default."
        ),
    )

    p = sub.add_parser("first-run-needed")
    p.add_argument("cwd", nargs="?", default=".")

    p = sub.add_parser("ensure-gitignore")
    p.add_argument("cwd", nargs="?", default=".")

    p = sub.add_parser("company-slug")
    p.add_argument("name")

    p = sub.add_parser(
        "build-prompt",
        help=(
            "Build a fully-substituted sub-agent prompt and emit to stdout. "
            "Orchestrators should dispatch sub-agents with the output of this "
            "command instead of substituting {vars} themselves — cross-host "
            "testing showed LLM-side substitution is unreliable."
        ),
    )
    p.add_argument(
        "--kind",
        required=True,
        choices=sorted(_PROMPT_KINDS),
        help="Which sub-agent prompt to build.",
    )
    p.add_argument(
        "--run-dir",
        default=None,
        help=(
            "Path to .resumasher/run/ (contains resume.txt, context.txt, "
            "jd.txt). Defaults to <cwd>/.resumasher/run."
        ),
    )
    p.add_argument(
        "--cwd",
        default=".",
        help=(
            "Student's working directory (contains .resumasher/cache.txt). "
            "Defaults to current directory."
        ),
    )
    p.add_argument(
        "--out-dir",
        default=None,
        help=(
            "Output directory for this application (contains "
            "company-research.md, tailored-resume.md). Required for the "
            "cover-letter kind."
        ),
    )
    p.add_argument(
        "--company",
        default=None,
        help="Company name. Required for company-researcher kind.",
    )
    p.add_argument(
        "--today",
        default=None,
        help=(
            "ISO date (YYYY-MM-DD) to use for the cover letter's date "
            "line. Defaults to today. Test-only override; in production "
            "the orchestrator does not pass this."
        ),
    )

    args = parser.parse_args()

    if args.command == "parse-job-mode":
        res = parse_job_source(args.arg, cwd=Path(args.cwd))
        # Single word, no newline trickery — safe to capture in $(...)
        # under any shell. End with sys.stdout.write rather than print to
        # avoid the platform-specific trailing newline that some shells
        # strip and others preserve in the captured value.
        sys.stdout.write(res.mode)
        return 0

    if args.command == "parse-job-content":
        res = parse_job_source(args.arg, cwd=Path(args.cwd))
        # Raw bytes on stdout — no JSON wrap, no escaping. The caller
        # pipes this directly into format-jd. UTF-8 by default; the
        # stdout reconfigure at the bottom of this file ensures
        # Windows-CP1252 doesn't crash on non-ASCII content (em-dashes,
        # curly quotes, em-dashes that LinkedIn copy/paste produces).
        sys.stdout.write(res.content)
        return 0

    if args.command == "format-jd":
        if args.content_file == "-":
            content = sys.stdin.read()
        else:
            content = Path(args.content_file).read_text(encoding="utf-8")
        sys.stdout.write(format_jd(args.mode, content, args.url))
        return 0

    if args.command == "discover-resume":
        path = discover_resume(Path(args.cwd))
        if path is None:
            print(
                "FAILURE: no resume found. Looked for these filenames in "
                + str(Path(args.cwd).resolve())
                + ": "
                + ", ".join(RESUME_CANDIDATES)
            )
            return 1
        print(str(path))
        return 0

    if args.command == "validate-resume-path":
        path, err = validate_resume_path(Path(args.cwd), args.filename)
        if err is not None:
            print(f"FAILURE: {err}", file=sys.stderr)
            return 1
        print(str(path))
        return 0

    if args.command == "folder-state-hash":
        print(folder_state_hash(Path(args.cwd)))
        return 0

    if args.command == "mine-context":
        cwd = Path(args.cwd)
        folder_prose = mine_folder_context(cwd)
        parts = [folder_prose]
        if args.github_username:
            # Import lazily so folder-only runs don't pay the import cost.
            import github_mine as gm

            def _persist_warning(msg: str) -> None:
                """
                Write the warning to a file so it survives trace rollup in
                non-Claude hosts (Codex truncates long stderr blocks and
                summarizes them into a paraphrased history entry). The file
                is a durable ground-truth record the student can paste into
                a bug report.
                """
                try:
                    run_dir = cwd / ".resumasher" / "run"
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "github-mine-error.txt").write_text(msg, encoding="utf-8")
                except OSError:
                    pass  # best-effort; don't fail the mine just because we can't log

            try:
                github_prose = gm.mine_github(args.github_username, cwd=cwd)
                parts.append(github_prose)
            except gm.RateLimitError as exc:
                msg = (
                    f"=== GITHUB_MINE_WARNING ===\n"
                    f"GitHub rate limit hit; continuing without GitHub evidence.\n"
                    f"Install `gh` and run `gh auth login` for a 5000/hr limit.\n"
                    f"Details: {exc}\n"
                )
                print("\n" + msg, file=sys.stderr)
                _persist_warning(msg)
            except gm.NotFoundError:
                msg = (
                    f"=== GITHUB_MINE_WARNING ===\n"
                    f"GitHub user '{args.github_username}' not found or has "
                    f"no public repos. Continuing without GitHub evidence.\n"
                )
                print("\n" + msg, file=sys.stderr)
                _persist_warning(msg)
            except gm.APIError as exc:
                msg = (
                    f"=== GITHUB_MINE_WARNING ===\n"
                    f"GitHub API error: {exc}\n"
                    f"Continuing without GitHub evidence. "
                    f"Full error written to .resumasher/run/github-mine-error.txt\n"
                )
                print("\n" + msg, file=sys.stderr)
                _persist_warning(msg)
        print("\n\n".join(parts))
        return 0

    if args.command == "github-mine":
        import github_mine as gm
        try:
            prose = gm.mine_github(
                args.username,
                cwd=Path(args.cwd),
                cap=args.cap,
                use_cache=not args.no_cache,
            )
        except gm.RateLimitError as exc:
            print(f"FAILURE: rate limit: {exc}", file=sys.stderr)
            return 2
        except gm.NotFoundError:
            print(f"FAILURE: user '{args.username}' not found", file=sys.stderr)
            return 3
        except gm.APIError as exc:
            print(f"FAILURE: {exc}", file=sys.stderr)
            return 4
        print(prose)
        return 0

    if args.command == "read-resume":
        # Defensive: weak models sometimes invoke read-resume in a fresh
        # Bash call where the prior call's $RESUME_PATH didn't persist
        # (observed under qwen3.6-35b on OpenCode). The argument then
        # arrives empty and Path("") resolves to "." (cwd), producing a
        # confusing IsADirectoryError deep in the call stack while the
        # caller blithely continues with a 0-byte resume.txt. Catch it
        # at the boundary instead.
        if not args.path or not args.path.strip():
            print(
                "FAILURE: read-resume requires a non-empty path argument. "
                "Likely cause: $RESUME_PATH was not re-derived in this "
                "Bash call. Re-run discover-resume in the same call and "
                "pass its output directly.",
                file=sys.stderr,
            )
            return 2
        print(read_resume(Path(args.path)))
        return 0

    if args.command == "extract-company":
        text = sys.stdin.read()
        company = extract_company(text)
        if company is None:
            print("")
            return 1
        print(company)
        return 0

    if args.command == "extract-role":
        text = sys.stdin.read()
        role = extract_role(text)
        if role is None:
            print("")
            return 1
        print(role)
        return 0

    if args.command == "extract-job-fields":
        # Read the job-extractor text once and persist each value to its
        # own file under --output-dir. The per-field-file shape replaces
        # writing key=value lines to an env file and shell-sourcing them —
        # that pattern breaks when company / role contain spaces (e.g.
        # "Elevation Capital" → bash parses "Capital" as a command).
        # See issue #50.
        text = sys.stdin.read()
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Each value is written as raw text — `$(cat file)` in the
        # consuming shell strips the trailing newline but preserves all
        # interior chars (spaces, ampersands, single quotes, backticks),
        # so the values round-trip byte-perfect regardless of contents.
        # Missing values render as empty files; the agent decides how to
        # handle "" downstream (the existing UNKNOWN sentinel handling).
        fields = {
            "company.txt": extract_company(text),
            "role.txt": extract_role(text),
        }
        for filename, value in fields.items():
            target = out_dir / filename
            target.write_text(
                "" if value is None else str(value), encoding="utf-8"
            )

        # Term lists get one term per line rather than the pipe-separated
        # wire format, so downstream readers (keyword-coverage, a human
        # inspecting the run dir) don't have to re-parse the separator.
        hard = extract_hard_requirements(text)
        preferred = extract_preferred(text)
        titles = extract_title_variants(text)
        for filename, terms in (
            ("hard-requirements.txt", hard),
            ("preferred.txt", preferred),
            ("title-variants.txt", titles),
        ):
            (out_dir / filename).write_text(
                "\n".join(terms) + ("\n" if terms else ""), encoding="utf-8"
            )

        # Pre-rendered block for the `jd_keywords` prompt variable, so
        # build-prompt doesn't have to re-derive the formatting.
        (out_dir / "keywords.txt").write_text(
            render_jd_keywords(hard, preferred, titles), encoding="utf-8"
        )

        # Stdout summary so the caller can sanity-check at the bash level
        # without re-cat-ing every file. Flat key=value so there's no
        # shell-eats-JSON repeat of issue #44.
        for key in ("company", "role"):
            v = fields[f"{key}.txt"]
            sys.stdout.write(f"{key}={'' if v is None else v}\n")
        sys.stdout.write(f"hard_requirements={len(hard)}\n")
        sys.stdout.write(f"preferred={len(preferred)}\n")
        return 0

    if args.command == "keyword-coverage":
        job_dir = Path(args.job_dir)
        resume_text = _read_if_exists(Path(args.resume))
        if resume_text is None:
            print(f"FAILURE: no such file: {args.resume}", file=sys.stderr)
            return 2

        def _terms(name: str) -> list[str]:
            raw = _read_if_exists(job_dir / name)
            if not raw:
                return []
            return [line.strip() for line in raw.splitlines() if line.strip()]

        hard_cov = keyword_coverage(_terms("hard-requirements.txt"), resume_text)
        pref_cov = keyword_coverage(_terms("preferred.txt"), resume_text)

        if args.json:
            print(json.dumps(
                {"hard_requirements": hard_cov, "preferred": pref_cov},
                ensure_ascii=False, indent=2,
            ))
            return 0

        for label, cov in (("Required", hard_cov), ("Preferred", pref_cov)):
            if not cov["total"]:
                continue
            print(
                f"{label}: {cov['matched_count']}/{cov['total']} terms present "
                f"({cov['percent']}%)"
            )
            if cov["missing"]:
                for term in cov["missing"]:
                    print(f"  missing: {term}")
        if not hard_cov["total"] and not pref_cov["total"]:
            print("No screening terms were extracted for this posting.")
        return 0

    if args.command == "lint-output":
        doc = _read_if_exists(Path(args.input))
        if doc is None:
            print(f"FAILURE: no such file: {args.input}", file=sys.stderr)
            return 2
        findings = lint_output(doc, kind=args.kind)
        if args.json:
            print(json.dumps(findings, ensure_ascii=False, indent=2))
            return 0
        if not findings:
            print(f"{Path(args.input).name}: clean")
            return 0
        for w in findings:
            print(f"[{w['code']}] {w['text']}")
            print(f"    {w['hint']}")
        return 0

    if args.command == "is-failure":
        text = sys.stdin.read()
        return 0 if is_failure_sentinel(text) else 1

    if args.command == "append-history":
        record = json.loads(args.json_line)
        path = append_history(Path(args.cwd), record)
        print(str(path))
        return 0

    if args.command == "cleanup-stray-prompts":
        actions = cleanup_stray_prompts(
            since_timestamp=args.since_timestamp,
            scan_dir=Path(args.scan_dir),
        )
        summary = {
            "scanned": str(Path(args.scan_dir).resolve()),
            "actions": [
                {
                    "path": str(a.path),
                    "action": a.action,
                    "reason": a.reason,
                }
                for a in actions
            ],
            "deleted": sum(1 for a in actions if a.action == "deleted"),
            "skipped": sum(1 for a in actions if a.action == "skipped"),
        }
        print(json.dumps(summary))
        return 0

    if args.command == "first-run-needed":
        needed = first_run_needed(Path(args.cwd))
        print("yes" if needed else "no")
        return 0 if needed else 1

    if args.command == "ensure-gitignore":
        path = ensure_gitignore(Path(args.cwd))
        print(str(path) if path else "")
        return 0

    if args.command == "company-slug":
        print(company_slug(args.name))
        return 0

    if args.command == "build-prompt":
        return _cmd_build_prompt(args)

    return 1


# ---------------------------------------------------------------------------
# build-prompt CLI handler
# ---------------------------------------------------------------------------


def _read_if_exists(path: Path) -> Optional[str]:
    """Read a file's text if it exists; return None otherwise. Never raises."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return None
    except OSError:
        return None


def _cmd_build_prompt(args: argparse.Namespace) -> int:
    """
    Resolve the variables the requested kind needs by reading files in
    $RUN_DIR / $CWD / $OUT_DIR, then call prompts.build_prompt. Emit the
    fully-substituted prompt to stdout.

    The file paths are conventional:
      - $RUN_DIR/resume.txt   — read-resume output
      - $RUN_DIR/context.txt  — mine-context output (raw folder+github)
      - $RUN_DIR/jd.txt       — JD text from parse-job-content piped through format-jd
      - $CWD/.resumasher/cache.txt — folder-miner sub-agent's prose summary
      - $OUT_DIR/company-research.md — company-researcher sub-agent output
      - $OUT_DIR/tailored-resume.md  — tailor sub-agent output

    If a required file is missing, exits 2 with an actionable error message
    naming the file and the phase that was supposed to have produced it.
    """
    cwd = Path(args.cwd).resolve()
    run_dir = Path(args.run_dir).resolve() if args.run_dir else cwd / ".resumasher" / "run"
    out_dir = Path(args.out_dir).resolve() if args.out_dir else None

    spec = _PROMPT_KINDS[args.kind]

    # Assemble the kwargs build_prompt accepts. Only the keys in
    # spec.required_vars will actually be substituted; others are ignored.
    kwargs: dict[str, Optional[str]] = {
        "resume_text": None,
        "folder_context": None,
        "folder_summary": None,
        "jd_text": None,
        "jd_keywords": None,
        "company": None,
        "company_research": None,
        "tailored_resume": None,
        "today_date": None,
    }

    def _missing(var: str, expected_path: Path, produced_by: str) -> int:
        print(
            f"FAILURE: build-prompt --kind {args.kind} requires variable "
            f"{var!r}, expected at {expected_path}. This file is produced "
            f"by {produced_by}. Run that phase first, or pass an explicit "
            f"--run-dir / --out-dir if the file is elsewhere.",
            file=sys.stderr,
        )
        return 2

    for var in spec.required_vars:
        if var == "resume_text":
            content = _read_if_exists(run_dir / "resume.txt")
            if content is None:
                return _missing(var, run_dir / "resume.txt", "orchestration read-resume in Phase 1")
            kwargs[var] = content
        elif var == "folder_context":
            content = _read_if_exists(run_dir / "context.txt")
            if content is None:
                return _missing(var, run_dir / "context.txt", "orchestration mine-context in Phase 2")
            kwargs[var] = content
        elif var == "folder_summary":
            content = _read_if_exists(cwd / ".resumasher" / "cache.txt")
            if content is None:
                return _missing(var, cwd / ".resumasher" / "cache.txt", "the folder-miner sub-agent in Phase 2")
            kwargs[var] = content
        elif var == "jd_text":
            content = _read_if_exists(run_dir / "jd.txt")
            if content is None:
                return _missing(var, run_dir / "jd.txt", "orchestration parse-job-content piped through format-jd in Phase 1")
            kwargs[var] = content
        elif var == "jd_keywords":
            content = _read_if_exists(run_dir / "job" / "keywords.txt")
            if content is None:
                return _missing(
                    var, run_dir / "job" / "keywords.txt",
                    "the job-extractor sub-agent piped through "
                    "`orchestration extract-job-fields` in Phase 3",
                )
            kwargs[var] = content
        elif var == "company":
            if not args.company:
                print(
                    f"FAILURE: build-prompt --kind {args.kind} requires --company <name>.",
                    file=sys.stderr,
                )
                return 2
            kwargs[var] = args.company
        elif var == "company_research":
            if out_dir is None:
                print(
                    f"FAILURE: build-prompt --kind {args.kind} requires --out-dir <path>.",
                    file=sys.stderr,
                )
                return 2
            content = _read_if_exists(out_dir / "company-research.md")
            if content is None:
                return _missing(var, out_dir / "company-research.md", "the company-researcher sub-agent in Phase 3")
            kwargs[var] = content
        elif var == "tailored_resume":
            if out_dir is None:
                print(
                    f"FAILURE: build-prompt --kind {args.kind} requires --out-dir <path>.",
                    file=sys.stderr,
                )
                return 2
            content = _read_if_exists(out_dir / "tailored-resume.md")
            if content is None:
                return _missing(var, out_dir / "tailored-resume.md", "the tailor sub-agent in Phase 4")
            kwargs[var] = content
        elif var == "contact_info":
            # Read configured contact fields from .resumasher/config.json and
            # format as a pre-built 2-line header the tailor must copy verbatim.
            # This exists because tailor sub-agents on some hosts (observed
            # under Gemini) don't have access to config — they'd otherwise
            # emit [INSERT LINKEDIN URL] placeholders or fall back to the
            # resume's stale location. With contact_info pre-formatted here,
            # the tailor has no ambiguity and no way to drift.
            config_path = cwd / ".resumasher" / "config.json"
            config_text = _read_if_exists(config_path)
            if config_text is None:
                return _missing(
                    var, config_path,
                    "first-run setup in Phase 0 (writes .resumasher/config.json)",
                )
            try:
                config = json.loads(config_text)
            except json.JSONDecodeError as exc:
                print(
                    f"FAILURE: build-prompt --kind {args.kind}: "
                    f"could not parse {config_path}: {exc}",
                    file=sys.stderr,
                )
                return 2
            try:
                kwargs[var] = _format_contact_info(
                    name=config.get("name", ""),
                    email=config.get("email", ""),
                    phone=config.get("phone", ""),
                    linkedin=config.get("linkedin", ""),
                    location=config.get("location", ""),
                )
            except ValueError as exc:
                print(
                    f"FAILURE: build-prompt --kind {args.kind}: {exc}. "
                    f"Fix the 'name' field in {config_path} and re-run.",
                    file=sys.stderr,
                )
                return 2
        elif var == "today_date":
            # Pre-format today's date so the cover-letter sub-agent can't
            # invent or misformat it. LLMs are unreliable about the current
            # date (no real-time clock, occasional drift), and a cover
            # letter dated last year is a credibility failure the student
            # wouldn't notice until after sending. Pinning it here removes
            # the entire failure mode. US business-letter convention
            # "Month D, YYYY" — the most universally readable English
            # format across US/EU/APAC recruiters. Format the day by hand
            # rather than relying on platform-specific strftime tokens
            # (%-d on POSIX vs %#d on Windows).
            today = _date.today() if args.today is None else _date.fromisoformat(args.today)
            kwargs[var] = f"{today.strftime('%B')} {today.day}, {today.year}"

    prompt = _build_prompt(args.kind, **kwargs)
    sys.stdout.write(prompt)
    # No trailing newline beyond whatever the template ends with; orchestrators
    # pasting this into a sub-agent dispatch don't want spurious whitespace.
    return 0


if __name__ == "__main__":
    # Python on Windows defaults stdin/stdout/stderr to the system ANSI code
    # page (typically CP1252) when not attached to a TTY. Prompts and JD
    # content contain `→`, `…`, curly quotes (U+2019), em-dashes (U+2014),
    # ligatures (ﬁ in 'office'), and non-ASCII names that CP1252 can't
    # represent. Without this reconfigure, three failure modes hit Windows
    # students:
    #  - stdout writes raise UnicodeEncodeError
    #  - stdin reads decode bytes via CP1252 with surrogateescape error
    #    handler, producing low-surrogates that can't later round-trip
    #    back to UTF-8 (this is the failure surfaced by issue #44's bash-
    #    pipeline test on the Windows CI matrix)
    #  - stderr writes corrupt diagnostics
    # Force UTF-8 on all three streams so the Windows Git Bash path
    # behaves identically to macOS/Linux.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(_cli())
