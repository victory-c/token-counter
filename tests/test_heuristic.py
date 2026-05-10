from __future__ import annotations

from tokenburn.classifier.heuristic import classify
from tokenburn.classifier.signals import SessionFeatures
from tokenburn.classifier.taxonomy import TaskCategory


def _feat(**kw) -> SessionFeatures:
    base = SessionFeatures(provider="claude_code", session_id="t")
    for k, v in kw.items():
        setattr(base, k, v)
    return base


def test_extraction_csv_no_edits_short_prompt():
    f = _feat(
        keywords_present={"extract", "list all"},
        edit_count=0,
        write_count=0,
        read_count=1,
        user_message_count=2,
        turn_count=3,
        file_extensions_touched={".csv"},
    )
    cls = classify(f)
    assert cls.category is TaskCategory.EXTRACTION


def test_summarization_long_input_short_session():
    f = _feat(
        keywords_present={"summarize"},
        first_user_message_chars=8000,
        edit_count=0,
        write_count=0,
        user_message_count=1,
        turn_count=2,
    )
    cls = classify(f)
    assert cls.category is TaskCategory.SUMMARIZATION


def test_code_review_read_heavy_no_edits():
    f = _feat(
        keywords_present={"review"},
        read_count=8,
        edit_count=0,
        write_count=0,
        bash_count=0,
        turn_count=4,
    )
    cls = classify(f)
    assert cls.category is TaskCategory.CODE_REVIEW


def test_feature_implementation_many_edits_many_files():
    f = _feat(
        keywords_present={"implement", "add"},
        edit_count=8,
        write_count=2,
        files_touched=6,
        bash_count=3,
        todowrite_count=1,
        turn_count=20,
        duration_seconds=1800,
        file_extensions_touched={".py", ".ts"},
    )
    cls = classify(f)
    assert cls.category is TaskCategory.FEATURE_IMPLEMENTATION


def test_bug_fix_targeted_change_with_tests():
    f = _feat(
        keywords_present={"fix", "bug"},
        edit_count=2,
        bash_count=2,
        files_touched=2,
        read_count=3,
        file_extensions_touched={".py"},
    )
    cls = classify(f)
    assert cls.category is TaskCategory.BUG_FIX


def test_debugging_lots_of_reads_and_runs_few_edits():
    f = _feat(
        keywords_present={"why", "investigate"},
        read_count=10,
        bash_count=6,
        edit_count=1,
        turn_count=15,
    )
    cls = classify(f)
    assert cls.category is TaskCategory.DEBUGGING


def test_planning_pure_conversation_no_tools():
    f = _feat(
        keywords_present={"plan", "design", "approach"},
        edit_count=0,
        write_count=0,
        read_count=0,
        bash_count=0,
        user_message_count=4,
        turn_count=4,
        user_message_total_chars=2500,
    )
    cls = classify(f)
    assert cls.category is TaskCategory.PLANNING_DESIGN


def test_research_web_heavy():
    f = _feat(
        keywords_present={"compare", "documentation"},
        web_search_count=4,
        web_fetch_count=2,
        edit_count=0,
        write_count=0,
        turn_count=6,
    )
    cls = classify(f)
    assert cls.category is TaskCategory.RESEARCH


def test_unclassified_when_signal_is_zero():
    f = _feat(turn_count=1, total_tokens=200)
    cls = classify(f)
    assert cls.category is TaskCategory.UNCLASSIFIED
    assert cls.confidence == 0.0


def test_confidence_is_high_when_winner_clear():
    f = _feat(
        keywords_present={"extract", "parse", "list all"},
        edit_count=0,
        write_count=0,
        file_extensions_touched={".csv", ".json"},
        read_count=2,
        user_message_count=2,
        turn_count=3,
    )
    cls = classify(f)
    assert cls.category is TaskCategory.EXTRACTION
    assert cls.confidence > 0.6  # clear winner
