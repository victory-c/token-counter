"""Load model_classes.yaml + task_fitness.yaml and answer:
  - what class is this model in?
  - what's the cheapest acceptable class for this task?
  - what's the representative model for re-pricing?
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

ModelClass = Literal["light", "medium", "heavy"]
_CLASS_RANK: dict[str, int] = {"light": 0, "medium": 1, "heavy": 2}


@dataclass(frozen=True)
class FitnessRule:
    minimum_class: ModelClass
    rationale: str


@dataclass
class FitnessTable:
    classes: dict[ModelClass, list[str]]            # class -> list of model prefixes
    representatives: dict[ModelClass, str]
    fitness: dict[str, FitnessRule]                  # task_category -> rule

    @classmethod
    def load(cls, classes_path: Path, fitness_path: Path) -> "FitnessTable":
        with classes_path.open() as f:
            mc = yaml.safe_load(f) or {}
        with fitness_path.open() as f:
            tf = yaml.safe_load(f) or {}
        # Sort each class's prefix list by length descending so longest-prefix
        # match wins (e.g. "claude-haiku-4-5" before "claude-haiku").
        sorted_classes: dict[ModelClass, list[str]] = {}
        for klass, items in (mc.get("classes") or {}).items():
            sorted_classes[klass] = sorted([s for s in items if s], key=len, reverse=True)
        representatives = mc.get("representatives") or {}
        fitness = {
            cat: FitnessRule(
                minimum_class=rule.get("minimum_class", "medium"),
                rationale=rule.get("rationale", ""),
            )
            for cat, rule in (tf.get("fitness") or {}).items()
        }
        return cls(
            classes=sorted_classes,
            representatives=representatives,
            fitness=fitness,
        )

    def class_of(self, model: str | None) -> ModelClass | None:
        if not model:
            return None
        m = model.lower()
        for klass, prefixes in self.classes.items():
            for prefix in prefixes:
                if m.startswith(prefix):
                    return klass  # type: ignore[return-value]
        return None

    def minimum_class_for(self, task_category: str) -> ModelClass:
        rule = self.fitness.get(task_category)
        return rule.minimum_class if rule else "medium"

    def rationale_for(self, task_category: str) -> str:
        rule = self.fitness.get(task_category)
        return rule.rationale if rule else ""

    def representative_for(self, klass: ModelClass) -> str | None:
        return self.representatives.get(klass)

    def is_overshooting(self, model: str | None, task_category: str) -> bool:
        """True if the model used is in a heavier class than the task needs."""
        actual = self.class_of(model)
        if actual is None:
            return False
        target = self.minimum_class_for(task_category)
        return _CLASS_RANK[actual] > _CLASS_RANK[target]


def default_fitness_path() -> tuple[Path, Path]:
    """Return (model_classes.yaml, task_fitness.yaml) bundled with the package."""
    pkg = Path(__file__).resolve().parent
    return pkg / "model_classes.yaml", pkg / "task_fitness.yaml"


def load_default() -> FitnessTable:
    mc, tf = default_fitness_path()
    return FitnessTable.load(mc, tf)
