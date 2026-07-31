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


# ---------------------------------------------------------------------------
# sanitize_dashes — the hard no-em-dash guarantee
# ---------------------------------------------------------------------------


def test_sanitize_replaces_spaced_em_dash_with_comma():
    from scripts.orchestration import sanitize_dashes

    out, changes = sanitize_dashes("The model — a gradient booster — ran nightly.")
    assert "\u2014" not in out
    assert out == "The model, a gradient booster, ran nightly."
    assert len(changes) == 1
    assert changes[0]["line"] == 1


def test_sanitize_uses_en_dash_for_numeric_ranges():
    """'2020—2024' is a range, not punctuation. A comma there would be
    wrong; an en dash is the typographically correct form."""
    from scripts.orchestration import sanitize_dashes

    out, _ = sanitize_dashes("Worked there 2020—2024 on pricing.")
    assert "\u2014" not in out
    assert "2020\u20132024" in out


def test_sanitize_leaves_clean_text_untouched():
    from scripts.orchestration import sanitize_dashes

    doc = "Built Power BI dashboards, cutting refresh time 40%.\n"
    out, changes = sanitize_dashes(doc)
    assert out == doc
    assert changes == []


def test_sanitize_preserves_en_dash_date_ranges():
    """The resume date format uses en dashes deliberately. Sanitizing em
    dashes must not disturb them."""
    from scripts.orchestration import sanitize_dashes

    doc = "### Data Analyst - Acme (Mar 2022 \u2013 Aug 2024)"
    out, changes = sanitize_dashes(doc)
    assert out == doc
    assert changes == []


def test_sanitize_does_not_leave_doubled_punctuation():
    from scripts.orchestration import sanitize_dashes

    out, _ = sanitize_dashes("One thing mattered —, the definitions.")
    assert ",," not in out
    assert " ," not in out


def test_sanitize_preserves_trailing_newline():
    from scripts.orchestration import sanitize_dashes

    out, _ = sanitize_dashes("A — B\n")
    assert out.endswith("\n")
    out2, _ = sanitize_dashes("A — B")
    assert not out2.endswith("\n")


def test_cli_sanitize_dashes_rewrites_file(tmp_path: Path):
    f = tmp_path / "cover.md"
    f.write_text("I built it — and it worked.\n", encoding="utf-8")
    r = _run(["sanitize-dashes", "--input", str(f)])
    assert r.returncode == 0, r.stderr
    assert "\u2014" not in f.read_text(encoding="utf-8")
    assert "rewrote 1 line" in r.stdout


def test_cli_sanitize_dashes_check_mode_does_not_write(tmp_path: Path):
    f = tmp_path / "cover.md"
    original = "I built it — and it worked.\n"
    f.write_text(original, encoding="utf-8")
    r = _run(["sanitize-dashes", "--input", str(f), "--check"])
    assert r.returncode == 1
    assert f.read_text(encoding="utf-8") == original


def test_cli_sanitize_dashes_check_mode_exits_0_when_clean(tmp_path: Path):
    f = tmp_path / "cover.md"
    f.write_text("No dashes here.\n", encoding="utf-8")
    r = _run(["sanitize-dashes", "--input", str(f), "--check"])
    assert r.returncode == 0
    assert "no em dashes" in r.stdout


# ---------------------------------------------------------------------------
# bulletize — every resume bullet ships as "• ", not "- "
# ---------------------------------------------------------------------------


def test_bulletize_converts_dash_markers():
    from scripts.orchestration import bulletize

    out, changes = bulletize("- Built the churn model.\n- Shipped it.\n")
    assert out == "• Built the churn model.\n• Shipped it.\n"
    assert [c["line"] for c in changes] == [1, 2]


def test_bulletize_converts_star_and_plus_markers():
    from scripts.orchestration import bulletize

    out, _ = bulletize("* One\n+ Two\n")
    assert out == "• One\n• Two\n"


def test_bulletize_preserves_indentation():
    from scripts.orchestration import bulletize

    out, _ = bulletize("  - nested bullet\n")
    assert out == "  • nested bullet\n"


def test_bulletize_leaves_hyphens_inside_text_alone():
    """The rule is about the line marker, not about hyphens."""
    from scripts.orchestration import bulletize

    out, _ = bulletize("- A/B-tested the end-to-end pipeline, Power BI-based.\n")
    assert out == "• A/B-tested the end-to-end pipeline, Power BI-based.\n"


def test_bulletize_leaves_horizontal_rules_and_headings_alone():
    from scripts.orchestration import bulletize

    doc = "# Ana Silva\n\n---\n\n### Data Analyst - Acme (Mar 2022 – Aug 2024)\n"
    out, changes = bulletize(doc)
    assert out == doc
    assert changes == []


def test_bulletize_skips_fenced_code_blocks():
    from scripts.orchestration import bulletize

    doc = "```bash\n- not a bullet\n```\n- a bullet\n"
    out, changes = bulletize(doc)
    assert "- not a bullet" in out
    assert "• a bullet" in out
    assert len(changes) == 1


def test_bulletize_is_idempotent():
    from scripts.orchestration import bulletize

    once, _ = bulletize("- Built the churn model.\n")
    twice, changes = bulletize(once)
    assert twice == once
    assert changes == []


def test_bulletize_preserves_trailing_newline():
    from scripts.orchestration import bulletize

    out, _ = bulletize("- A\n")
    assert out.endswith("\n")
    out2, _ = bulletize("- A")
    assert not out2.endswith("\n")


def test_bulletize_converts_skills_lines():
    """The skills section is a list too — 'Category: item, item' lines
    get the same marker as experience bullets."""
    from scripts.orchestration import bulletize

    out, _ = bulletize("## Skills\n- Programming: Python, SQL, R\n")
    assert "• Programming: Python, SQL, R" in out


def test_cli_bulletize_rewrites_file(tmp_path: Path):
    f = tmp_path / "tailored-resume.md"
    f.write_text("- Built the churn model.\n", encoding="utf-8")
    r = _run(["bulletize", "--input", str(f)])
    assert r.returncode == 0, r.stderr
    assert f.read_text(encoding="utf-8") == "• Built the churn model.\n"


def test_cli_bulletize_check_mode_does_not_write(tmp_path: Path):
    f = tmp_path / "tailored-resume.md"
    original = "- Built the churn model.\n"
    f.write_text(original, encoding="utf-8")
    r = _run(["bulletize", "--input", str(f), "--check"])
    assert r.returncode == 1
    assert f.read_text(encoding="utf-8") == original


def test_cli_bulletize_check_mode_exits_0_when_already_converted(tmp_path: Path):
    f = tmp_path / "tailored-resume.md"
    f.write_text("• Built the churn model.\n", encoding="utf-8")
    r = _run(["bulletize", "--input", str(f), "--check"])
    assert r.returncode == 0


def test_tailor_prompt_specifies_the_bullet_character():
    """The deterministic pass is the guarantee, but the prompt has to ask
    for it too — otherwise every run rewrites every line."""
    from scripts.prompts import TAILOR_PROMPT

    assert "## Bullet marker" in TAILOR_PROMPT
    assert "• Built the churn model" in TAILOR_PROMPT
    assert "• Category: item, item, item" in TAILOR_PROMPT
    assert "- bullet" not in TAILOR_PROMPT


# ---------------------------------------------------------------------------
# Cover-letter prompt: the requirements the student specified
# ---------------------------------------------------------------------------


def _cover_prompt(relocation: str = "(none)") -> str:
    from scripts.prompts import build_prompt

    return build_prompt(
        "cover-letter",
        contact_info="# Ana\na@x.com",
        today_date="May 2, 2026",
        tailored_resume="R",
        jd_text="J",
        jd_keywords="K",
        company_research="C",
        folder_summary="E",
        relocation_context=relocation,
    )


def test_cover_prompt_specifies_three_to_four_paragraphs():
    p = _cover_prompt()
    assert "Three to four body paragraphs" in p
    assert "One page maximum" in p


def test_cover_prompt_carries_the_storytelling_chain():
    """The student's central ask: situation encountered -> conclusion
    drawn -> resulting interest in a specific task in this posting."""
    p = _cover_prompt()
    assert "THE MOST IMPORTANT PARAGRAPH" in p
    assert "I encountered [specific situation]" in p
    assert "which showed me [specific conclusion drawn]" in p
    assert "interests me" in p


def test_cover_prompt_forbids_inventing_the_story():
    p = _cover_prompt()
    assert "Do not invent an" in p and "experience to make a better story" in p


def test_cover_prompt_requires_a_named_recipient():
    p = _cover_prompt()
    assert "To Whom It May" in p  # named in order to forbid it
    assert "LinkedIn" in p


def test_cover_prompt_permits_the_conventional_european_openers():
    """The student explicitly asked for these. They were on the banned
    list after the AI-detection pass; the resolution is that they're
    permitted WITH a specific clause."""
    p = _cover_prompt()
    assert "I am writing to apply for the Financial Analyst position" in p
    assert "I would welcome the opportunity to discuss" in p
    assert "The second half of that sentence is what makes it work" in p


def test_cover_prompt_bans_overclaiming():
    p = _cover_prompt()
    for phrase in ("I am the best fit for this role.",
                   "I will revolutionize your processes."):
        assert phrase in p
    assert "My experience enables me to contribute to improving" in p


def test_cover_prompt_includes_relocation_block_when_set():
    p = _cover_prompt("Non-EU citizen, post-study permit in Austria.")
    assert "Non-EU citizen, post-study permit in Austria." in p
    assert "{relocation_context}" not in p


def test_relocation_guidance_forbids_inventing_visa_status():
    """Misstating someone's right to work is not a style problem."""
    p = _cover_prompt("x")
    assert "Never state a permit, visa, or eligibility status that is not" in p


def test_relocation_guidance_rejects_tourism_reasons():
    p = _cover_prompt("x")
    assert "Not the weather, not the food" in p
    assert "Signal duration" in p


def test_conventional_formulas_are_advisory_not_errors():
    """These are the student's requested formulations. The linter must
    not report them under the same code as a genuine AI tell."""
    findings = lint_output(
        "I am writing to apply for the Analyst role.", kind="cover-letter"
    )
    codes = {f["code"] for f in findings}
    assert "CHECK_SPECIFICITY" in codes
    assert "AI_TELL_PHRASE" not in codes


def test_linter_flags_to_whom_it_may_concern():
    findings = lint_output("To Whom It May Concern,", kind="cover-letter")
    assert "UNNAMED_RECIPIENT" in {f["code"] for f in findings}


def test_linter_flags_overclaiming():
    findings = lint_output("I am the best fit for this role.", kind="cover-letter")
    assert "AI_TELL_PHRASE" in {f["code"] for f in findings}
