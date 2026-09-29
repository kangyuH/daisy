"""Task semantic contract (docs/daisy-task-contract.template.yaml).

The YAML document is the commitment. ``revision`` is not stored here;
``tasks.contract_revision`` is incremented by the Gateway on each save.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.services.tasks.models import TaskValidationError

SCHEMA_VERSION = "0.1"

AgreementStatus = Literal[
    "confirmed",
    "provisional",
    "open",
    "conflicted",
    "not_applicable",
]
SourceRole = Literal["trigger", "supporting", "parent_task"]
SourceKind = Literal["feishu_message", "ticket", "task", "other"]
DecisionMode = Literal["self_check", "human_approval"]
DestinationKind = Literal["feishu_thread", "ticket", "parent_task", "other"]
ActorKind = Literal["person", "team", "system"]
PartyRole = Literal["requester", "approver", "recipient", "external_support"]


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected a string or null")
    text = value.strip()
    return text or None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(_Strict):
    role: SourceRole
    kind: SourceKind
    ref: Optional[str] = None
    note: Optional[str] = None

    @field_validator("ref", "note", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class Subject(_Strict):
    status: AgreementStatus = "open"
    object_type: Optional[str] = None
    object_ref: Optional[str] = None
    occurrence: Optional[str] = None
    description: Optional[str] = None
    authority_party_id: Optional[str] = None
    basis_refs: list[str] = Field(default_factory=list)

    @field_validator(
        "object_type",
        "object_ref",
        "occurrence",
        "description",
        "authority_party_id",
        mode="before",
    )
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class Objective(_Strict):
    status: AgreementStatus = "open"
    statement: Optional[str] = None
    intended_use: Optional[str] = None
    authority_party_id: Optional[str] = None
    basis_refs: list[str] = Field(default_factory=list)

    @field_validator("statement", "intended_use", "authority_party_id", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class ScopeItem(_Strict):
    dimension: Optional[str] = None
    value: Optional[str] = None
    status: AgreementStatus = "open"
    authority_party_id: Optional[str] = None
    basis_refs: list[str] = Field(default_factory=list)

    @field_validator("dimension", "value", "authority_party_id", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class Scope(_Strict):
    completeness: AgreementStatus = "open"
    included: list[ScopeItem] = Field(default_factory=list)
    excluded: list[ScopeItem] = Field(default_factory=list)
    constraints: list[ScopeItem] = Field(default_factory=list)


class ResultCriterion(_Strict):
    id: Optional[str] = None
    statement: Optional[str] = None
    evidence_required: list[str] = Field(default_factory=list)
    status: AgreementStatus = "open"
    authority_party_id: Optional[str] = None
    basis_refs: list[str] = Field(default_factory=list)

    @field_validator("id", "statement", "authority_party_id", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class Deliverable(_Strict):
    name: Optional[str] = None
    required_content: list[str] = Field(default_factory=list)
    format: Optional[str] = None
    status: AgreementStatus = "open"
    authority_party_id: Optional[str] = None
    basis_refs: list[str] = Field(default_factory=list)

    @field_validator("name", "format", "authority_party_id", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class Decision(_Strict):
    status: AgreementStatus = "open"
    mode: Optional[DecisionMode] = None
    approver_party_ids: list[str] = Field(default_factory=list)
    rule: Optional[str] = None
    basis_refs: list[str] = Field(default_factory=list)

    @field_validator("mode", "rule", mode="before")
    @classmethod
    def _blank_mode(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class InvestigationExit(_Strict):
    status: AgreementStatus = "open"
    checks_required: list[str] = Field(default_factory=list)
    report_required: list[str] = Field(default_factory=list)
    approver_party_ids: list[str] = Field(default_factory=list)
    basis_refs: list[str] = Field(default_factory=list)


class Acceptance(_Strict):
    completeness: AgreementStatus = "open"
    result_criteria: list[ResultCriterion] = Field(default_factory=list)
    deliverables: list[Deliverable] = Field(default_factory=list)
    decision: Decision = Field(default_factory=Decision)
    investigation_exit: InvestigationExit = Field(default_factory=InvestigationExit)


class PartyRefList(_Strict):
    status: AgreementStatus = "open"
    party_ids: list[str] = Field(default_factory=list)
    basis_refs: list[str] = Field(default_factory=list)


class Destination(_Strict):
    status: AgreementStatus = "open"
    kind: Optional[DestinationKind] = None
    ref: Optional[str] = None
    basis_refs: list[str] = Field(default_factory=list)

    @field_validator("kind", "ref", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class FeedbackContent(_Strict):
    status: AgreementStatus = "open"
    requirements: list[str] = Field(default_factory=list)
    basis_refs: list[str] = Field(default_factory=list)


class Feedback(_Strict):
    recipients: PartyRefList = Field(default_factory=PartyRefList)
    destination: Destination = Field(default_factory=Destination)
    content: FeedbackContent = Field(default_factory=FeedbackContent)


class Actor(_Strict):
    kind: Optional[ActorKind] = None
    ref: Optional[str] = None
    name: Optional[str] = None

    @field_validator("kind", "ref", "name", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class Party(_Strict):
    party_id: str
    actor: Actor = Field(default_factory=Actor)
    roles: list[PartyRole] = Field(default_factory=list)
    responsibility: Optional[str] = None
    expected_input: list[str] = Field(default_factory=list)
    decision_dependencies: list[str] = Field(default_factory=list)

    @field_validator("party_id", mode="before")
    @classmethod
    def _pid(cls, value: Any) -> str:
        text = _opt_str(value)
        if not text:
            raise ValueError("party_id is required")
        return text

    @field_validator("responsibility", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class CompetingClaim(_Strict):
    statement: Optional[str] = None
    basis_refs: list[str] = Field(default_factory=list)

    @field_validator("statement", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class Gap(_Strict):
    id: Optional[str] = None
    question: Optional[str] = None
    impact: Optional[str] = None
    clarifier_party_ids: list[str] = Field(default_factory=list)
    can_do_before_resolution: list[str] = Field(default_factory=list)
    cannot_commit_before_resolution: list[str] = Field(default_factory=list)
    competing_claims: list[CompetingClaim] = Field(default_factory=list)
    basis_refs: list[str] = Field(default_factory=list)

    @field_validator("id", "question", "impact", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class Origin(_Strict):
    initial_problem: Optional[str] = None
    sources: list[Source] = Field(default_factory=list)

    @field_validator("initial_problem", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)


class TaskContract(_Strict):
    schema_version: str = SCHEMA_VERSION
    task_id: Optional[str] = None
    title: Optional[str] = None
    origin: Origin = Field(default_factory=Origin)
    subject: Subject = Field(default_factory=Subject)
    objective: Objective = Field(default_factory=Objective)
    scope: Scope = Field(default_factory=Scope)
    acceptance: Acceptance = Field(default_factory=Acceptance)
    feedback: Feedback = Field(default_factory=Feedback)
    parties: list[Party] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {value!r}; expected {SCHEMA_VERSION!r}"
            )
        return value

    @field_validator("task_id", "title", mode="before")
    @classmethod
    def _blank(cls, value: Any) -> Optional[str]:
        return _opt_str(value)

    @model_validator(mode="after")
    def _party_refs(self) -> "TaskContract":
        ids = [party.party_id for party in self.parties]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate party_id")
        known = set(ids)
        refs: list[tuple[str, Optional[str]]] = [
            ("subject.authority_party_id", self.subject.authority_party_id),
            ("objective.authority_party_id", self.objective.authority_party_id),
        ]
        for bucket, items in (
            ("scope.included", self.scope.included),
            ("scope.excluded", self.scope.excluded),
            ("scope.constraints", self.scope.constraints),
        ):
            for index, item in enumerate(items):
                refs.append((f"{bucket}[{index}].authority_party_id", item.authority_party_id))
        for index, item in enumerate(self.acceptance.result_criteria):
            refs.append(
                (f"acceptance.result_criteria[{index}].authority_party_id", item.authority_party_id)
            )
        for index, item in enumerate(self.acceptance.deliverables):
            refs.append(
                (f"acceptance.deliverables[{index}].authority_party_id", item.authority_party_id)
            )
        for pid in self.acceptance.decision.approver_party_ids:
            refs.append(("acceptance.decision.approver_party_ids", pid))
        for pid in self.acceptance.investigation_exit.approver_party_ids:
            refs.append(("acceptance.investigation_exit.approver_party_ids", pid))
        for pid in self.feedback.recipients.party_ids:
            refs.append(("feedback.recipients.party_ids", pid))
        for index, gap in enumerate(self.gaps):
            for pid in gap.clarifier_party_ids:
                refs.append((f"gaps[{index}].clarifier_party_ids", pid))
        missing = [f"{where}={pid!r}" for where, pid in refs if pid and pid not in known]
        if missing:
            raise ValueError("unknown party_id: " + ", ".join(missing))
        return self

    def document(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def task_contract_id(task_id: int) -> str:
    return f"task_{int(task_id)}"


def parse_contract(payload: Any, *, task_id: int) -> TaskContract:
    if not isinstance(payload, dict):
        raise TaskValidationError("contract must be an object")
    if "revision" in payload or "contract_revision" in payload:
        raise TaskValidationError("revision is managed by the API")
    try:
        contract = TaskContract.model_validate(payload)
    except ValidationError as exc:
        raise TaskValidationError(str(exc)) from exc
    except ValueError as exc:
        raise TaskValidationError(str(exc)) from exc
    contract.task_id = task_contract_id(task_id)
    return contract


def seed_contract(
    *,
    task_id: int,
    title: str,
    one_liner: str = "",
    thread_id: Optional[str] = None,
    created_from_inbound_id: Optional[int] = None,
) -> dict[str, Any]:
    """Initial contract for a newly created task. Caller text stays provisional."""
    statement = (one_liner or "").strip() or (title or "").strip() or None
    origin = Origin()
    if created_from_inbound_id is not None:
        origin.sources.append(
            Source(
                role="trigger",
                kind="other",
                ref=f"inbound:{int(created_from_inbound_id)}",
            )
        )
    feedback = Feedback()
    thread = (thread_id or "").strip()
    if thread:
        feedback.destination = Destination(
            status="provisional",
            kind="feishu_thread",
            ref=thread,
        )
    contract = TaskContract(
        task_id=task_contract_id(task_id),
        title=(title or "").strip() or None,
        origin=origin,
        objective=Objective(status="provisional", statement=statement),
        feedback=feedback,
    )
    return contract.document()


def projection_updates(contract: TaskContract) -> dict[str, str]:
    """Columns derived from the contract. Blank statement does not clear one_liner.

    A provisional feishu thread does not overwrite the routing thread_id.
    """
    updates: dict[str, str] = {}
    title = (contract.title or "").strip()
    if title:
        updates["title"] = title
    statement = (contract.objective.statement or "").strip()
    if statement:
        updates["one_liner"] = statement
    dest = contract.feedback.destination
    ref = (dest.ref or "").strip()
    if dest.status == "confirmed" and dest.kind == "feishu_thread" and ref:
        updates["thread_id"] = ref
    return updates
