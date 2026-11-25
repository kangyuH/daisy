"""Immutable inputs and transactional memory ledger for dynamic task replay."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from phase0_eval.dataset import Key, Record, Text, unique_index

Period = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
NewRef = Annotated[str, StringConstraints(pattern=r"^new:[A-Za-z0-9][A-Za-z0-9_.-]*$")]


class Excerpt(Record):
    quote: Text
    source: str | None = None


class Event(Record):
    id: Key
    time: str
    thread: Key
    text: Text
    speaker: str = ""
    source: str | None = None
    context: list[Excerpt] = Field(default_factory=list)
    replay_of: Key | None = None


class Stream(Record):
    id: Key
    kind: Literal["real", "synthetic"]
    events: list[Event]


class GrowthBundle(Record):
    schema_version: Literal[1] = Field(alias="schema")
    project: Text
    map: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    streams: list[Stream]
    provenance: dict

    @model_validator(mode="after")
    def check_order(self):
        unique_index(self.streams, "stream")
        if not self.streams:
            raise ValueError("at least one stream is required")
        for stream in self.streams:
            seen = {}
            previous = None
            if not stream.events:
                raise ValueError(f"{stream.id}: stream is empty")
            for event in stream.events:
                stamp = datetime.fromisoformat(event.time.replace("Z", "+00:00"))
                if stamp.utcoffset() is None or previous is not None and stamp < previous:
                    raise ValueError(f"{stream.id}/{event.id}: expected chronological times with UTC offset")
                previous = stamp
                if event.id in seen:
                    raise ValueError(f"{stream.id}: duplicate event ID {event.id}")
                if event.replay_of:
                    original = seen.get(event.replay_of)
                    if (original is None or original.replay_of is not None
                            or (event.text, event.thread) != (original.text, original.thread)):
                        raise ValueError(f"{stream.id}/{event.id}: invalid replay source")
                seen[event.id] = event
        return self


class Route(Record):
    target: Text
    quote: Text
    kind: Literal["evidence", "maintain", "pending"]


class Create(Record):
    op: Literal["create"]
    ref: NewRef
    parent: Text
    kind: Literal["period", "work"]
    title: Text
    period: Period | None
    outcome: Text
    done_when: Text
    quote: Text
    why: Text


class Update(Record):
    op: Literal["update"]
    target: Text
    action: Literal["progress", "wait", "resume", "complete", "reopen", "contract"]
    quote: Text
    why: Text
    progress: Text | None = None
    wait_for: Text | None = None
    outcome: Text | None = None
    done_when: Text | None = None


class Move(Record):
    op: Literal["move"]
    message_id: Key
    target: Text
    quote: Text
    why: Text
    source_quote: Text | None = None


Change = Annotated[Create | Update | Move, Field(discriminator="op")]


class Proposal(Record):
    routes: list[Route] = Field(max_length=12)
    changes: list[Change] = Field(max_length=12)
    reason: Text


ROOT_ID = "root"


def project_seed(project: str) -> dict:
    return {
        "id": ROOT_ID, "parent": None, "kind": "root", "title": project,
        "period": None, "outcome": "承接项目消息和待判断事项", "done_when": "持续维护",
        "status": "active", "progress": None, "wait_for": None, "created_from": None,
    }


class GrowthState:
    def __init__(self, project: str):
        self.tasks = {ROOT_ID: project_seed(project)}
        self.links: list[dict] = []
        self.processed: set[str] = set()

    def snapshot(self) -> dict:
        return deepcopy({
            "tasks": list(self.tasks.values()),
            "links": self.links,
            "open_pending": [row for row in self.links if row["kind"] == "pending"],
        })

    def apply(self, event_id: str, text: str, proposal: Proposal) -> tuple[dict, list[str], list[str]]:
        if event_id in self.processed:
            raise ValueError("message already replayed")
        tasks = deepcopy(self.tasks)
        links = deepcopy(self.links)
        refs = dict.fromkeys(tasks)
        old_pending = [row for row in links if row["kind"] == "pending"]
        changes = proposal.changes
        created, updates, routes, moves = [], [], [], []
        actions = set()
        periods = set()

        def target(ref):
            if ref not in refs:
                raise ValueError(f"unknown task reference {ref!r}")
            return refs[ref] or ref

        def mark_period(*ids):
            periods.update(tasks[i]["period"] for i in ids if tasks[i]["period"])

        for row in [*proposal.routes, *changes]:
            if row.quote not in text:
                raise ValueError("operation quote must be taken from the current message")

        # Resolve aliases in proposal order. A child can refer to an earlier
        # creation in the same transaction.
        for change in changes:
            if not isinstance(change, Create):
                continue
            parent_id = target(change.parent)
            parent = tasks[parent_id]
            if change.ref in refs:
                raise ValueError("same new alias used twice")
            if change.kind == "period":
                if parent_id != ROOT_ID or change.period is None:
                    raise ValueError("a period node needs a business period and project root as parent")
                if any(t["kind"] == "period" and t["period"] == change.period for t in tasks.values()):
                    raise ValueError("a task for this business period already exists")
                actions.add("create_period")
            else:
                if parent["period"] is not None and parent["period"] != change.period:
                    raise ValueError("child period must match the parent")
                if any(t["kind"] == "period" and t["period"] == change.period for t in tasks.values()) and parent_id == ROOT_ID:
                    raise ValueError("period work belongs under its existing period node")
                normalized_title = " ".join(change.title.casefold().split())
                if any(t["parent"] == parent_id and t["period"] == change.period
                       and " ".join(t["title"].casefold().split()) == normalized_title
                       for t in tasks.values()):
                    raise ValueError("an identically titled sibling already exists for this period")
                actions.add("create_child")
            key = f"t{len(tasks):03d}"
            refs[change.ref] = key
            task = change.model_dump(exclude={"op", "ref", "quote", "why"})
            task.update(id=key, parent=parent_id, status="active", progress=None,
                        wait_for=None, created_from=event_id)
            tasks[key] = task
            created.append(deepcopy(task) | {"quote": change.quote, "why": change.why})
            actions.add("create")
            mark_period(key)

        route_keys = set()
        for route in proposal.routes:
            dest = target(route.target)
            if (dest == ROOT_ID) != (route.kind == "pending"):
                raise ValueError("a pending route must target the root, and a root route is pending")
            key = (route.quote, dest)
            if key in route_keys:
                raise ValueError("duplicate evidence relation within the proposal")
            route_keys.add(key)
            row = route.model_dump() | {"target": dest, "message_id": event_id}
            routes.append(row)
            links.append(row)
            actions.add("pending" if dest == ROOT_ID else "attach")
            if route.kind == "evidence":
                actions.add("evidence")
            mark_period(dest)

        routed = {row["target"] for row in routes}
        edit_keys = set()
        for change in changes:
            if isinstance(change, Create):
                created_id = refs[change.ref]
                child_has_evidence = change.kind == "period" and any(
                    tasks[child_id]["parent"] == created_id for child_id in routed
                )
                if created_id not in routed and not child_has_evidence:
                    raise ValueError("new tasks must have a source route or a grounded period child")
                continue
            dest = target(change.target)
            if dest == ROOT_ID or dest not in routed:
                raise ValueError("maintenance needs a current source route to a specific task")
            task = tasks[dest]
            if isinstance(change, Move):
                if change.message_id == event_id:
                    raise ValueError("a migration must reference earlier evidence")
                previous = [
                    row for row in old_pending if row["message_id"] == change.message_id
                    and (change.source_quote is None or row["quote"] == change.source_quote)
                ]
                if len(previous) != 1 or previous[0] not in links:
                    raise ValueError("migration must reference one unresolved earlier root relation")
                old = previous[0]
                links.remove(old)
                resolved = old | {"target": dest, "kind": "evidence"}
                if resolved not in links:
                    links.append(resolved)
                moves.append(change.model_dump(exclude={"op", "source_quote"}) | {
                    "target": dest, "source_quote": old["quote"],
                })
                actions.add("move")
                mark_period(dest)
                continue

            action = change.action
            key = (dest, action)
            if key in edit_keys:
                raise ValueError("duplicate update action on the same task")
            edit_keys.add(key)
            before_status = task["status"]
            if action == "wait":
                if not change.wait_for or before_status == "done":
                    raise ValueError("waiting needs a blocker on an unfinished task")
                task.update(status="waiting", wait_for=change.wait_for)
            elif action == "resume":
                if before_status != "waiting":
                    raise ValueError("only a waiting task can resume")
                task.update(status="active", wait_for=None)
            elif action == "complete":
                if before_status == "done":
                    raise ValueError("attach repeated completion as evidence")
                task.update(status="done", wait_for=None)
            elif action == "reopen":
                if before_status != "done":
                    raise ValueError("only a completed task can reopen")
                task.update(status="active", wait_for=None)
            elif action == "progress":
                if not change.progress:
                    raise ValueError("progress needs a concrete note")
            elif action == "contract":
                if not (change.outcome or change.done_when):
                    raise ValueError("contract update needs a changed result or acceptance")
                for field in ("outcome", "done_when"):
                    value = getattr(change, field)
                    if value is not None:
                        task[field] = value
            if change.progress is not None:
                task["progress"] = change.progress
            updates.append(change.model_dump(exclude={"op"}, exclude_none=True) | {
                "target": dest, "previous_status": before_status, "status": task["status"],
            })
            actions.add(action)
            mark_period(dest)

        self.tasks, self.links = tasks, links
        self.processed.add(event_id)
        return {"created": created, "updates": updates, "routes": routes, "moves": moves}, sorted(actions or {"ignore"}), sorted(periods)
