"""Rules-based session classifier.

Each category has a small scorer. The highest-scoring category wins.
Confidence = winner_score / (winner_score + runner_up_score), so a clear
winner approaches 1.0 and a coin-flip is around 0.5.

Why a decision tree, not ML: explainability. When a user says "this got
mis-classified" we can point at the specific rule that fired, edit it,
and re-run. That feedback loop is impossible with a black-box model.
"""
from __future__ import annotations

from dataclasses import dataclass

from .signals import SessionFeatures
from .taxonomy import CATEGORY_KEYWORDS, TaskCategory

_CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
             ".cpp", ".c", ".h", ".swift", ".kt", ".rb", ".php"}
_DATA_EXT = {".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml", ".sql"}

# Minimum winning score to declare a category. Below this, the session has
# only weak structural signals (e.g. "no edits") with no keywords or tool use,
# and we'd rather mark it unclassified than over-claim. Tuned so concrete
# category fixtures all score >= 5 and the zero-signal session falls below.
_MIN_WINNING_SCORE = 3.5


@dataclass
class Classification:
    category: TaskCategory
    confidence: float
    scores: dict[TaskCategory, float]


def classify(feat: SessionFeatures) -> Classification:
    scores: dict[TaskCategory, float] = {
        TaskCategory.EXTRACTION: _score_extraction(feat),
        TaskCategory.SUMMARIZATION: _score_summarization(feat),
        TaskCategory.CODE_REVIEW: _score_code_review(feat),
        TaskCategory.FEATURE_IMPLEMENTATION: _score_feature_impl(feat),
        TaskCategory.BUG_FIX: _score_bug_fix(feat),
        TaskCategory.DEBUGGING: _score_debugging(feat),
        TaskCategory.PLANNING_DESIGN: _score_planning(feat),
        TaskCategory.RESEARCH: _score_research(feat),
    }

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    winner, top = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

    # Below the minimum threshold (or at zero) → no real signal. Mark
    # unclassified so we don't over-claim on a session that just happens
    # to have a couple of structural bonuses fire.
    if top < _MIN_WINNING_SCORE:
        return Classification(
            category=TaskCategory.UNCLASSIFIED,
            confidence=0.0,
            scores=scores,
        )

    denom = top + runner_up
    confidence = round(top / denom, 3) if denom > 0 else 1.0
    return Classification(category=winner, confidence=confidence, scores=scores)


# ---------------------------------------------------------------------------
# Per-category scorers
# ---------------------------------------------------------------------------


def _kw_hit(feat: SessionFeatures, category: TaskCategory) -> int:
    """Count how many of `category`'s keywords showed up in user messages."""
    return sum(1 for kw in CATEGORY_KEYWORDS[category] if kw in feat.keywords_present)


def _score_extraction(f: SessionFeatures) -> float:
    s = 0.0
    s += _kw_hit(f, TaskCategory.EXTRACTION) * 1.5
    if f.edit_count == 0 and f.write_count == 0 and f.apply_patch_count == 0:
        s += 2  # no code changes
    if f.file_extensions_touched & _DATA_EXT:
        s += 2
    if f.read_count >= 1 and f.user_message_count <= 3:
        s += 1
    if f.turn_count and f.turn_count <= 5:
        s += 1
    return s


def _score_summarization(f: SessionFeatures) -> float:
    s = 0.0
    s += _kw_hit(f, TaskCategory.SUMMARIZATION) * 2
    if f.first_user_message_chars > 2000:
        s += 1.5  # long pasted-in input
    if f.edit_count == 0 and f.write_count == 0 and f.apply_patch_count == 0:
        s += 1
    if f.user_message_count <= 3 and f.turn_count and f.turn_count <= 4:
        s += 1
    if not (f.file_extensions_touched & _CODE_EXT):
        s += 0.5  # not really a code task
    return s


def _score_code_review(f: SessionFeatures) -> float:
    s = 0.0
    s += _kw_hit(f, TaskCategory.CODE_REVIEW) * 2
    if f.read_count >= 3 and f.edit_count == 0:
        s += 2
    if f.read_count > f.edit_count + f.write_count and f.read_count >= 2:
        s += 1
    if f.bash_count == 0:
        s += 0.5
    return s


def _score_feature_impl(f: SessionFeatures) -> float:
    s = 0.0
    if f.edit_count + f.write_count + f.apply_patch_count >= 5:
        s += 3
    if f.files_touched >= 3:
        s += 2
    if f.bash_count >= 2 or f.exec_command_count >= 2:
        s += 1
    if f.todowrite_count >= 1:
        s += 1
    s += _kw_hit(f, TaskCategory.FEATURE_IMPLEMENTATION) * 1.5
    if f.duration_seconds > 600:  # >10min
        s += 1
    if f.file_extensions_touched & _CODE_EXT:
        s += 1
    return s


def _score_bug_fix(f: SessionFeatures) -> float:
    s = 0.0
    s += _kw_hit(f, TaskCategory.BUG_FIX) * 2.5  # strongest keyword signal
    if 1 <= f.edit_count + f.apply_patch_count <= 4:
        s += 1.5  # targeted change
    if f.bash_count >= 1 or f.exec_command_count >= 1:
        s += 1  # ran tests
    if f.files_touched and f.files_touched <= 3:
        s += 1
    if f.read_count >= 1 and f.read_count <= 5:
        s += 0.5
    return s


def _score_debugging(f: SessionFeatures) -> float:
    s = 0.0
    s += _kw_hit(f, TaskCategory.DEBUGGING) * 2
    if f.bash_count >= 3 or f.exec_command_count >= 3:
        s += 1.5
    if f.read_count >= 4 and f.edit_count <= 2:
        s += 1.5
    if f.turn_count >= 8:
        s += 1
    return s


def _score_planning(f: SessionFeatures) -> float:
    s = 0.0
    s += _kw_hit(f, TaskCategory.PLANNING_DESIGN) * 2
    if f.edit_count == 0 and f.write_count == 0 and f.apply_patch_count == 0:
        s += 1.5
    if f.read_count == 0 and f.bash_count == 0 and f.exec_command_count == 0:
        s += 1  # pure conversation
    if f.user_message_total_chars > 1000 and f.turn_count and f.turn_count <= 6:
        s += 1
    return s


def _score_research(f: SessionFeatures) -> float:
    s = 0.0
    if f.web_search_count + f.web_fetch_count >= 2:
        s += 3
    if f.web_search_count + f.web_fetch_count >= 1:
        s += 1
    s += _kw_hit(f, TaskCategory.RESEARCH) * 1.5
    if f.edit_count == 0 and f.write_count == 0:
        s += 0.5
    return s
