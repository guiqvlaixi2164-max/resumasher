"""
Tests for the two screening-oriented deterministic helpers:

  * keyword_coverage — which JD screening terms made it into the resume
  * lint_output      — machine-written tells and unresolved authoring markup

Both exist because the tailor/cover-letter prompts ASK for the right
behaviour and a weaker model will ignore the ask. These are the belt to
that suspenders, and they cost zero tokens, so they run on every artifact
of every run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.orchestration import (
    AI_TELL_PHRASES,
    extract_hard_requirements,
    extract_preferred,
    extract_title_variants,
    keyword_coverage,
    lint_output,
    render_jd_keywords,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Term-list extraction from the job-extractor's output
# ---------------------------------------------------------------------------


SAMPLE_EXTRACTOR = (
    "COMPANY: Deloitte Consulting\n"
    "ROLE: Data Analyst, Commercial\n"
    "HARD_REQUIREMENTS: SQL | Power BI | Natural Language Processing (NLP) | "
    "demand forecasting\n"
    "PREFERRED: Airflow | dbt\n"
    "TITLE_VARIANTS: Data Analyst, Commercial | Commercial Analyst\n"
)


def test_extract_hard_requirements_splits_on_pipe():
    assert extract_hard_requirements(SAMPLE_EXTRACTOR) == [
        "SQL",
        "Power BI",
        "Natural Language Processing (NLP)",
        "demand forecasting",
    ]


def test_terms_containing_commas_survive():
    """The separator is ' | ' precisely because requirement strings and
    role titles routinely contain commas. A comma split would shred
    'Data Analyst, Commercial' into two bogus terms."""
    assert extract_title_variants(SAMPLE_EXTRACTOR) == [
        "Data Analyst, Commercial",
        "Commercial Analyst",
    ]


def test_extract_preferred_handles_none_sentinel():
    assert extract_preferred("PREFERRED: none") == []
    assert extract_preferred("PREFERRED: N/A") == []
    assert extract_preferred("no preferred line at all") == []


def test_term_lists_tolerate_markdown_bold():
    """Same weak-model failure mode the COMPANY/ROLE extractors handle."""
    prose = "**HARD_REQUIREMENTS:** SQL | Power BI\n"
    assert extract_hard_requirements(prose) == ["SQL", "Power BI"]


def test_term_lists_dedupe_case_insensitively_keeping_jd_spelling():
    """The whole point of extraction is capturing the JD's surface form,
    so the FIRST spelling wins when a term repeats."""
    prose = "HARD_REQUIREMENTS: Power BI | power bi | POWER BI | SQL\n"
    assert extract_hard_requirements(prose) == ["Power BI", "SQL"]


def test_term_lists_strip_bullet_and_bold_residue():
    prose = "HARD_REQUIREMENTS: - SQL | **Power BI** | `dbt`\n"
    assert extract_hard_requirements(prose) == ["SQL", "Power BI", "dbt"]


def test_render_jd_keywords_marks_empty_sections_explicitly():
    """An omitted section reads to the LLM as 'this got truncated'. An
    explicit '(none listed)' reads as 'there were none'."""
    out = render_jd_keywords(["SQL"], [], [])
    assert "SQL" in out
    assert "(none listed)" in out


# ---------------------------------------------------------------------------
# keyword_coverage
# ---------------------------------------------------------------------------


RESUME = """# Ana Muller
ana@example.com | Vienna

## Summary
Analyst with demand forecasting experience.

## Experience
### Data Analyst - Acme (Mar 2022 - Aug 2024)
- Built Power BI dashboards on 2.3M rows of SQL data.
"""


def test_keyword_coverage_finds_present_terms():
    cov = keyword_coverage(["SQL", "Power BI", "demand forecasting"], RESUME)
    assert cov["missing"] == []
    assert cov["matched_count"] == 3
    assert cov["percent"] == 100.0


def test_keyword_coverage_reports_absent_terms():
    cov = keyword_coverage(["SQL", "Kubernetes"], RESUME)
    assert cov["missing"] == ["Kubernetes"]
    assert cov["matched"] == ["SQL"]


def test_keyword_coverage_is_case_and_punctuation_insensitive():
    """'Power-BI' and 'power bi' are the same term for coverage purposes.
    The prompt handles making the resume use the JD's exact spelling; the
    checker's job is to avoid false 'missing' reports on a spacing nit."""
    cov = keyword_coverage(["power-bi", "POWER BI"], RESUME)
    assert cov["missing"] == []


def test_keyword_coverage_matches_either_half_of_an_acronym_pair():
    """A JD writing 'Natural Language Processing (NLP)' is telling us the
    two forms are the same thing, so a resume with either one satisfies
    the requirement."""
    assert keyword_coverage(
        ["Natural Language Processing (NLP)"], "Applied NLP to support tickets."
    )["missing"] == []
    assert keyword_coverage(
        ["Natural Language Processing (NLP)"],
        "Applied Natural Language Processing to support tickets.",
    )["missing"] == []
    assert keyword_coverage(
        ["Natural Language Processing (NLP)"], "Built dashboards."
    )["missing"] == ["Natural Language Processing (NLP)"]


def test_keyword_coverage_empty_term_list_does_not_divide_by_zero():
    cov = keyword_coverage([], RESUME)
    assert cov == {
        "total": 0, "matched": [], "missing": [],
        "matched_count": 0, "missing_count": 0, "percent": 0.0,
    }


# ---------------------------------------------------------------------------
# lint_output — unresolved authoring markup
# ---------------------------------------------------------------------------


def test_lint_flags_unresolved_insert_placeholder():
    findings = lint_output("- Led [INSERT TEAM SIZE] engineers.")
    codes = [f["code"] for f in findings]
    assert "UNRESOLVED_PLACEHOLDER" in codes


def test_lint_flags_leftover_soft_comment():
    """The regression this exists for: HTML comments are invisible in a
    markdown preview but paste into Word as literal visible text, so a
    student who skips the edit step ships the annotation in their resume."""
    doc = "- Led a team. <!--SOFT: Led a senior data science org.-->"
    findings = lint_output(doc)
    codes = [f["code"] for f in findings]
    assert "SOFT_COMMENT_PRESENT" in codes


def test_lint_clean_document_returns_no_findings():
    doc = "- Built Power BI dashboards on 2.3M rows, cutting refresh time 40%."
    assert lint_output(doc) == []


# ---------------------------------------------------------------------------
# lint_output — machine-written tells
# ---------------------------------------------------------------------------


def test_lint_flags_em_dash():
    findings = lint_output("Analyst — and forecaster.")
    assert [f["code"] for f in findings] == ["EM_DASH"]


def test_lint_allows_en_dash_in_a_date_range():
    """'Mar 2022 – Aug 2024' is the format the tailor is told to use, so
    flagging it would fire on every correctly-formatted resume."""
    doc = "### Data Analyst - Acme (Mar 2022 – Aug 2024)"
    codes = [f["code"] for f in lint_output(doc)]
    assert "EN_DASH_IN_PROSE" not in codes


def test_lint_allows_en_dash_before_present():
    doc = "### Data Analyst - Acme (Mar 2022 – Present)"
    codes = [f["code"] for f in lint_output(doc)]
    assert "EN_DASH_IN_PROSE" not in codes


@pytest.mark.parametrize(
    "phrase",
    [
        "I am writing to express my interest in the role.",
        "A detail-oriented professional with a proven track record.",
        "I would welcome the opportunity to discuss further.",
        "We delve into the data.",
        "A seamless, best-in-class solution.",
    ],
)
def test_lint_flags_known_ai_tells(phrase):
    codes = [f["code"] for f in lint_output(phrase, kind="cover-letter")]
    assert "AI_TELL_PHRASE" in codes


def test_lint_ignores_tells_inside_code_blocks():
    """A student's resume may legitimately contain a code sample. Prose
    checks should not fire on it."""
    doc = "```\nresults_driven = df.query('detail-oriented')\n```\n"
    codes = [f["code"] for f in lint_output(doc)]
    assert "AI_TELL_PHRASE" not in codes


def test_lint_flags_uniform_sentence_length_in_cover_letter():
    para = (
        "I built the forecasting pipeline that your team is now hiring for here. "
        "It processed two million rows of transaction data every single night. "
        "The model reduced forecast error by twelve percent across four regions. "
        "My work supported the planning team throughout the entire fiscal year."
    )
    codes = [f["code"] for f in lint_output(para, kind="cover-letter")]
    assert "UNIFORM_SENTENCE_LENGTH" in codes


def test_lint_accepts_varied_sentence_length():
    para = (
        "I built the forecasting pipeline that your team is now hiring for here. "
        "It ran nightly. The model reduced forecast error by twelve percent "
        "across four regions, which the planning team used for its quarterly bets."
    )
    codes = [f["code"] for f in lint_output(para, kind="cover-letter")]
    assert "UNIFORM_SENTENCE_LENGTH" not in codes


def test_sentence_rhythm_checks_do_not_run_on_resumes():
    """Resume bullets are fragments. Sentence-rhythm analysis on them
    would fire constantly and mean nothing."""
    para = (
        "I built the forecasting pipeline that your team is now hiring for here. "
        "It processed two million rows of transaction data every single night. "
        "The model reduced forecast error by twelve percent across four regions. "
        "My work supported the planning team throughout the entire fiscal year."
    )
    codes = [f["code"] for f in lint_output(para, kind="resume")]
    assert "UNIFORM_SENTENCE_LENGTH" not in codes


def test_lint_flags_three_paragraphs_of_identical_length():
    """The template shape recruiters recognize on sight."""
    para = " ".join(["word"] * 60)
    doc = "\n\n".join([para, para, para])
    codes = [f["code"] for f in lint_output(doc, kind="cover-letter")]
    assert "UNIFORM_PARAGRAPH_LENGTH" in codes


def test_ai_tell_phrases_are_lowercase_for_casefold_matching():
    """lint_output casefolds the document once and does substring checks,
    so an uppercase entry in the list could never match."""
    for phrase in AI_TELL_PHRASES:
        assert phrase == phrase.lower(), f"{phrase!r} must be lowercase"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _run(args: list[str], stdin: str = "") -> "subprocess.CompletedProcess":
    return subprocess.run(
        [sys.executable, "-m", "scripts.orchestration", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        check=False,
    )


def test_cli_extract_job_fields_writes_term_files(tmp_path: Path):
    out = tmp_path / "job"
    r = _run(["extract-job-fields", "--output-dir", str(out)], SAMPLE_EXTRACTOR)
    assert r.returncode == 0, r.stderr
    assert (out / "hard-requirements.txt").read_text(encoding="utf-8").splitlines() == [
        "SQL", "Power BI", "Natural Language Processing (NLP)", "demand forecasting",
    ]
    assert (out / "preferred.txt").read_text(encoding="utf-8").splitlines() == ["Airflow", "dbt"]
    # keywords.txt is the pre-rendered prompt variable; build-prompt exits 2
    # for the tailor/cover-letter kinds without it.
    assert "Power BI" in (out / "keywords.txt").read_text(encoding="utf-8")
    assert "hard_requirements=4" in r.stdout


def test_cli_keyword_coverage_reports_missing(tmp_path: Path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "hard-requirements.txt").write_text("SQL\nKubernetes\n", encoding="utf-8")
    (job / "preferred.txt").write_text("", encoding="utf-8")
    resume = tmp_path / "r.md"
    resume.write_text(RESUME, encoding="utf-8")

    r = _run(["keyword-coverage", "--job-dir", str(job), "--resume", str(resume)])
    assert r.returncode == 0, r.stderr
    assert "1/2" in r.stdout
    assert "missing: Kubernetes" in r.stdout


def test_cli_keyword_coverage_json(tmp_path: Path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "hard-requirements.txt").write_text("SQL\n", encoding="utf-8")
    resume = tmp_path / "r.md"
    resume.write_text(RESUME, encoding="utf-8")

    r = _run(["keyword-coverage", "--job-dir", str(job), "--resume", str(resume), "--json"])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["hard_requirements"]["matched"] == ["SQL"]


def test_cli_lint_output_reports_findings(tmp_path: Path):
    f = tmp_path / "cover.md"
    f.write_text("I am writing to express my interest — truly.", encoding="utf-8")
    r = _run(["lint-output", "--input", str(f), "--kind", "cover-letter"])
    assert r.returncode == 0, r.stderr
    assert "EM_DASH" in r.stdout
    assert "AI_TELL_PHRASE" in r.stdout


def test_cli_lint_output_clean_file(tmp_path: Path):
    f = tmp_path / "r.md"
    f.write_text("- Built Power BI dashboards on 2.3M rows.", encoding="utf-8")
    r = _run(["lint-output", "--input", str(f)])
    assert r.returncode == 0
    assert "clean" in r.stdout


def test_cli_lint_output_missing_file_exits_2(tmp_path: Path):
    r = _run(["lint-output", "--input", str(tmp_path / "nope.md")])
    assert r.returncode == 2
    assert "FAILURE" in r.stderr


# ---------------------------------------------------------------------------
# Prompt/linter list synchronization
# ---------------------------------------------------------------------------


def test_banned_phrases_appear_in_the_prompts_too():
    """The linter catching a phrase the prompt never forbade means the
    model was never told. Spot-check that the two lists agree on the
    highest-signal entries."""
    from scripts.prompts import HUMAN_VOICE_RULES

    rules = HUMAN_VOICE_RULES.lower()
    for phrase in ("detail-oriented", "proven track record", "delve",
                   "seamless", "results-driven", "team player"):
        assert phrase in rules, (
            f"{phrase!r} is in AI_TELL_PHRASES but not in HUMAN_VOICE_RULES. "
            f"The linter would flag text the prompt never warned against."
        )


def test_voice_rules_reach_both_student_facing_prompts():
    from scripts.prompts import COVER_LETTER_PROMPT, TAILOR_PROMPT

    anchor = "does not read as machine-generated"
    assert anchor in TAILOR_PROMPT
    assert anchor in COVER_LETTER_PROMPT
