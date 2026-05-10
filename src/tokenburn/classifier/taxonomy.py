from __future__ import annotations

from enum import Enum

CLASSIFIER_VERSION = "heuristic-v1"


class TaskCategory(str, Enum):
    FEATURE_IMPLEMENTATION = "feature_implementation"
    BUG_FIX = "bug_fix"
    CODE_REVIEW = "code_review"
    DEBUGGING = "debugging"
    EXTRACTION = "extraction"
    SUMMARIZATION = "summarization"
    PLANNING_DESIGN = "planning_design"
    RESEARCH = "research"
    UNCLASSIFIED = "unclassified"


# Human-readable descriptions, used in `task-detail` output.
DESCRIPTIONS: dict[TaskCategory, str] = {
    TaskCategory.FEATURE_IMPLEMENTATION: "Adding new code, multi-file edits, end-to-end feature work.",
    TaskCategory.BUG_FIX: "Targeted change with test verification.",
    TaskCategory.CODE_REVIEW: "Read-heavy review with light edits and commentary.",
    TaskCategory.DEBUGGING: "Iterative read/run/edit cycles, error analysis.",
    TaskCategory.EXTRACTION: "Structured-output transformation; no code changes.",
    TaskCategory.SUMMARIZATION: "Condensing long input into a shorter form.",
    TaskCategory.PLANNING_DESIGN: "Architecture or design discussion; no code touches.",
    TaskCategory.RESEARCH: "Web search / fetch heavy; lightweight synthesis.",
    TaskCategory.UNCLASSIFIED: "Insufficient signal to classify.",
}


# Keyword bank used by signal extraction. Kept small + legible so misclassifications
# are easy to debug.
CATEGORY_KEYWORDS: dict[TaskCategory, tuple[str, ...]] = {
    TaskCategory.EXTRACTION: (
        "extract", "parse", "convert to", "list all", "find all",
        "json schema", "tabulate", "to csv",
    ),
    TaskCategory.SUMMARIZATION: (
        "summarize", "summary", "tldr", "boil down", "condense", "explain in one",
    ),
    TaskCategory.BUG_FIX: (
        "fix", "bug", "broken", "doesn't work", "regression", "traceback", "crashes",
    ),
    TaskCategory.CODE_REVIEW: (
        "review", "audit", "look at", "any issues", "feedback on", "code quality",
    ),
    TaskCategory.PLANNING_DESIGN: (
        "plan", "architect", "design", "approach", "tradeoff", "should i",
        "high level",
    ),
    TaskCategory.DEBUGGING: (
        "why", "investigate", "trace", "stuck", "what's wrong", "not working",
    ),
    TaskCategory.FEATURE_IMPLEMENTATION: (
        "implement", "add", "build", "create a", "write a", "feature",
    ),
    TaskCategory.RESEARCH: (
        "search", "look up", "documentation", "best practice", "compare",
    ),
}


def all_keywords() -> set[str]:
    """Flat set of all tracked keywords; used for cheap presence checks."""
    out: set[str] = set()
    for words in CATEGORY_KEYWORDS.values():
        out.update(words)
    return out
