"""Evaluation dataset: a document corpus plus questions labelled with *evidence strings*.

A question's ground truth is a list of short verbatim excerpts from the corpus, not chunk ids.
A retrieved chunk is relevant if it contains one of them. Labels therefore survive any change
to chunking, embeddings or ingestion - the very things the evaluation is meant to compare.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from corpus.core.generation import Turn

QuestionType = Literal["paraphrase", "keyword", "multi", "unanswerable", "conversational"]
QUESTION_TYPES: tuple[QuestionType, ...] = (
    "paraphrase",
    "keyword",
    "multi",
    "unanswerable",
    "conversational",
)


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    type: QuestionType
    question: str
    evidence: tuple[str, ...]
    answer: str | None
    history: tuple[Turn, ...] = ()

    @property
    def answerable(self) -> bool:
        return bool(self.evidence)


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    filename: str
    text: str


@dataclass(frozen=True, slots=True)
class Dataset:
    documents: tuple[CorpusDocument, ...]
    questions: tuple[Question, ...] = field(default_factory=tuple)

    def of_type(self, *types: QuestionType) -> list[Question]:
        return [q for q in self.questions if q.type in types]


def load_dataset(root: Path) -> Dataset:
    documents = tuple(
        CorpusDocument(p.name, p.read_text(encoding="utf-8"))
        for p in sorted((root / "corpus").glob("*.md"))
    )
    questions: list[Question] = []
    for line in (root / "questions.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw["type"] not in QUESTION_TYPES:
            raise ValueError(f"{raw['id']}: unknown question type {raw['type']!r}")
        questions.append(
            Question(
                id=raw["id"],
                type=raw["type"],
                question=raw["question"],
                evidence=tuple(raw.get("evidence", [])),
                answer=raw.get("answer"),
                history=tuple(Turn(t["role"], t["content"]) for t in raw.get("history", [])),
            )
        )
    return Dataset(documents=documents, questions=tuple(questions))
