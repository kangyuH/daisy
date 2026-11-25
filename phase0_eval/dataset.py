"""Validate and fingerprint the frozen routing inputs and separate annotations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Key = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Task(Record):
    id: Key
    parent: Key | None
    title: Text
    month: str | None
    status: Literal["noted", "done", "waiting_human", "waiting_external", "blocked"]
    objective: Text
    scope: Text
    acceptance: Text
    stop: Text


class Context(Record):
    id: Key
    text: Text
    speaker: str = ""
    task_refs: list[Key] = []


class Case(Record):
    id: Key
    group: Literal["dev", "holdout"]
    kind: Literal["real", "synthetic"]
    thread: Key
    source: Text
    text: Text
    context: list[Context]
    candidates: list[Key]
    tags: list[str]
    mentions: list[str] = []
    speaker: str = ""
    reply_to: Key | None = None
    gate: Literal["eligible", "ineligible", "unknown"] = "unknown"


class Bundle(Record):
    version: Literal[1]
    project: Text
    root: Key
    tasks: list[Task]
    cases: list[Case]
    provenance: dict

    @model_validator(mode="after")
    def check_references(self):
        tasks = unique_index(self.tasks, "task")
        unique_index(self.cases, "case")
        if self.root not in tasks or tasks[self.root].parent is not None:
            raise ValueError("root must identify a task with no parent")
        if not self.cases:
            raise ValueError("bundle contains no cases")
        for task in self.tasks:
            if task.month is not None:
                from datetime import date
                try:
                    date.fromisoformat(task.month + "-01")
                except ValueError as exc:
                    raise ValueError(f"{task.id}: month must be YYYY-MM") from exc
            node = task.id
            seen = set()
            while node != self.root:
                if node in seen or node not in tasks or tasks[node].parent is None:
                    raise ValueError(f"{task.id}: parent chain must reach root without a cycle")
                seen.add(node)
                node = tasks[node].parent
        thread_groups = {}
        for case in self.cases:
            if thread_groups.setdefault(case.thread, case.group) != case.group:
                raise ValueError(f"{case.id}: thread crosses development and holdout groups")
            context = unique_index(case.context, f"{case.id} context")
            if case.reply_to is not None and case.reply_to not in context:
                raise ValueError(f"{case.id}: reply_to is outside the frozen context")
            targets = set(case.candidates)
            if len(targets) != len(case.candidates) or targets - tasks.keys():
                raise ValueError(f"{case.id}: duplicate or unknown candidate")
            if self.root not in targets:
                raise ValueError(f"{case.id}: root must be a candidate")
            for message in case.context:
                if set(message.task_refs) - targets:
                    raise ValueError(f"{case.id}/{message.id}: context references a noncandidate")
        return self


class Relation(Record):
    quote: Text
    target: Key
    wake: bool


class Annotation(Record):
    options: list[list[Relation]]
    reason: Text


class Labels(Record):
    version: Text
    status: Literal["draft", "reviewed", "accepted"]
    cases: dict[Key, Annotation]
    provenance: dict | None = None


def unique_index(records, label):
    result = {}
    for record in records:
        if record.id in result:
            raise ValueError(f"duplicate {label} ID {record.id}")
        result[record.id] = record
    return result


def validate_labels(bundle: dict, labels: dict) -> None:
    data = Labels.model_validate(labels)
    cases = {case["id"]: case for case in bundle["cases"]}
    missing = sorted(cases.keys() - data.cases.keys())
    unknown = sorted(data.cases.keys() - cases.keys())
    if missing or unknown:
        raise ValueError(f"annotation IDs mismatch: missing={missing}, unknown={unknown}")
    for case_id, label in data.cases.items():
        case = cases[case_id]
        if not label.options:
            raise ValueError(f"{case_id}: at least one label option required")
        for option in label.options:
            seen = set()
            for relation in option:
                if relation.quote not in case["text"]:
                    raise ValueError(f"{case_id}: annotation quote is not a substring of the message")
                if relation.target not in case["candidates"]:
                    raise ValueError(f"{case_id}: invalid annotation target {relation.target}")
                key = (relation.quote, relation.target)
                if key in seen:
                    raise ValueError(f"{case_id}: duplicate annotation relation {key}")
                seen.add(key)


def fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_digest(data) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return fingerprint(raw.encode("utf-8"))


def no_duplicate_keys(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"duplicate JSON key {key!r}")
        data[key] = value
    return data


@dataclass(frozen=True)
class Input:
    path: Path
    digest: str
    data: dict

    @classmethod
    def read(cls, path: Path) -> Input:
        if not path.is_file():
            raise ValueError(f"missing input file: {path}")
        raw = path.read_bytes()
        data = json.loads(raw, object_pairs_hook=no_duplicate_keys)
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object")
        return cls(path, fingerprint(raw), data)

    def check_unchanged(self) -> None:
        if fingerprint(self.path.read_bytes()) != self.digest:
            raise ValueError(f"input file changed during evaluation: {self.path}")


def load_dataset(bundle_path: Path, labels_path: Path) -> tuple[Input, Input]:
    bundle = Input.read(bundle_path)
    labels = Input.read(labels_path)
    normalized = Bundle.model_validate(bundle.data).model_dump()
    validate_labels(normalized, labels.data)
    return Input(bundle.path, bundle.digest, normalized), labels
