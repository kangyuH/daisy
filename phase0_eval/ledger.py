"""Record the effects a route would have, without a task service."""

from __future__ import annotations

from dataclasses import dataclass, field


class SimulatedInterruption(RuntimeError):
    pass


@dataclass
class ReplayLedger:
    associations: set[tuple[str, str, str]] = field(default_factory=set)
    wakeups: set[tuple[str, str]] = field(default_factory=set)
    completed: set[str] = field(default_factory=set)

    def apply(self, event_id: str, relations: list[dict], *, interrupt: str | None = None) -> dict:
        if event_id in self.completed:
            return {"associations": 0, "wakeups": 0, "outbound": 0, "duplicate": True}

        links = {(event_id, row["quote"], row["target"]) for row in relations}
        owners = {(event_id, row["target"]) for row in relations if row["wake"] is True}
        if interrupt == "before_commit":
            raise SimulatedInterruption("before commit")
        delta = {"associations": len(links - self.associations),
                 "wakeups": len(owners - self.wakeups), "outbound": 0, "duplicate": False}
        self.associations.update(links)
        self.wakeups.update(owners)
        self.completed.add(event_id)
        if interrupt == "after_commit":
            raise SimulatedInterruption("after commit, before acknowledgement")
        return delta


def exercise_replay(event_id: str, relations: list[dict]) -> dict:
    ledger = ReplayLedger()
    first = ledger.apply(event_id, relations)
    duplicate = ledger.apply(event_id, relations)
    recovered = {}
    for point in ("before_commit", "after_commit"):
        retry_ledger = ReplayLedger()
        try:
            retry_ledger.apply(event_id, relations, interrupt=point)
        except SimulatedInterruption:
            recovered[point] = retry_ledger.apply(event_id, relations)
        if (retry_ledger.associations, retry_ledger.wakeups) != (ledger.associations, ledger.wakeups):
            raise AssertionError("retry produced different replay ledger effects")
    return {"first": first, "duplicate": duplicate, "retry": recovered}
