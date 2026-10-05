"""The sprint side of the PO service's resolver (`PoService.sprint_session`).

A sprint records the PO session that opened it (`sprint create --po-session`, revision 0016). When
that session no longer exists or is closed, the resolver opens a fresh one and seeds it from what
outlives a session: the sprint's why-document in the instance knowledge and the PO workspace's own
`NOTES.md`. This module is what the resolver needs of the sprint and nothing else: read its recorded
session, find its why-document, record the fresh session, and say so in the sprint's comments.

The same port serves the production rule of a dispatcher's submit (secretary-1764): the sprint's
`allowed_productions`, read and never written here. The rule refuses nothing and hands nothing over
(secretary-1769): the PO records an allowance itself (`sprint allow-production --role po`).

The service holds a :class:`SprintSessions`; :class:`BoardSprintSessions` is the installation's
board, and a unit test gives the service a fake one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Any

# The role and actor of what the resolver writes on a sprint: its comment and its session record.
RESOLVER_ROLE = "po"
RESOLVER_ACTOR = "po-service"
# Where an open-sprint skill writes a sprint's why-document, under the instance repository.
WHY_DOCUMENTS_RELATIVE = Path("state") / "knowledge" / "decisions"


@dataclass(frozen=True)
class SprintRecord:
    ref: str
    status: str
    po_session: str | None
    # The productions its operation cards may touch (`sprint create --allow-production`).
    allowed_productions: tuple[str, ...] = ()
    owner_decisions: tuple[dict[str, Any], ...] = ()
    comments: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class WhyDocument:
    """One decision document that names the sprint; `path` is relative to the instance repository."""

    path: str
    text: str


class SprintSessions(Protocol):
    def sprint(self, sprint_ref: str) -> SprintRecord | None: ...

    def why_documents(self, sprint_ref: str) -> list[WhyDocument]: ...

    def comment(self, sprint_ref: str, body: str, *, request_id: str) -> None: ...

    def record_po_session(self, sprint_ref: str, session_id: str, *, request_id: str) -> None: ...


def find_why_documents(instance_dir: Path | str, sprint_ref: str) -> list[WhyDocument]:
    """Every `state/knowledge/decisions/*.md` that names `sprint_ref` as a whole reference, by path.

    `sprint:14` does not name `sprint:1465`: the reference has to end where a word would.
    """
    root = Path(instance_dir).expanduser()
    directory = root / WHY_DOCUMENTS_RELATIVE
    if not directory.is_dir():
        return []
    pattern = re.compile(rf"(?<![\w:-]){re.escape(sprint_ref)}(?![\w-])")
    found = []
    for path in sorted(directory.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern.search(text):
            found.append(WhyDocument(str(path.relative_to(root)), text))
    return found


def why_document_label(documents: list[WhyDocument]) -> str:
    """What the sprint comment says the session was seeded with, besides NOTES.md."""
    if len(documents) == 1:
        return documents[0].path
    if not documents:
        return "no why-document found"
    return "no single why-document (" + ", ".join(document.path for document in documents) + ")"


def seed_message(sprint_ref: str, previous: str | None, why: str, documents: list[WhyDocument]) -> str:
    """The first input of a session the resolver opened: which sprint it serves, why, and its context."""
    if previous:
        opened = f"The sprint's recorded PO session {previous} {why}, so the PO service opened this one."
    else:
        opened = "The sprint recorded no PO session, so the PO service opened this one."
    lines = [
        f"This PO session serves {sprint_ref}. {opened}",
        "",
        f"Read NOTES.md in your workspace first. The live sprint is `ummanu sprint show --ref {sprint_ref}`.",
        "",
    ]
    if len(documents) == 1:
        lines += [f"The sprint's why-document, {documents[0].path}:", "", documents[0].text.rstrip()]
    elif not documents:
        lines.append(f"No why-document under {WHY_DOCUMENTS_RELATIVE}/ names {sprint_ref}.")
    else:
        lines.append(
            f"Several documents under {WHY_DOCUMENTS_RELATIVE}/ name {sprint_ref}, so none is quoted here:"
        )
        lines += [f"- {document.path}" for document in documents]
    return "\n".join(lines) + "\n"


def reseed_comment(previous: str | None, session_id: str, documents: list[WhyDocument]) -> str:
    return (
        f"the PO session {previous or 'none'} no longer exists; opened {session_id} seeded with "
        f"{why_document_label(documents)} and NOTES.md"
    )


class BoardSprintSessions:
    """The installation's sprints, as the resolver reads and writes them. Construction does no I/O."""

    def __init__(self, instance: Path | str, data_dir: Path | str) -> None:
        path = Path(instance).expanduser()
        self.instance = path.parent if path.name == "instance.yaml" else path
        self.data_dir = Path(data_dir)

    def _client(self):
        from ummanu.board.backend import SPRINT, board_client

        return board_client(self.instance, serves=(SPRINT,))

    def _writer(self):
        from ummanu.sprints import SprintWriter

        return SprintWriter(self._client(), data_dir=self.data_dir, instance=self.instance)

    def sprint(self, sprint_ref: str) -> SprintRecord | None:
        from ummanu.sprints import SprintReader
        from ummanu.tasks import TaskError

        try:
            document = SprintReader(self._client(), data_dir=self.data_dir).show(
                sprint_ref, include_cards=False, include_resume_freshness=False
            )
        except TaskError as exc:
            if exc.code == "not_found":
                return None
            raise
        return SprintRecord(
            str(document.get("ref") or sprint_ref),
            str(document.get("status") or ""),
            document.get("po_session") or None,
            tuple(str(project) for project in document.get("allowed_productions") or ()),
            tuple(document.get("owner_decisions") or ()),
            tuple(document.get("comments") or ()),
        )

    def why_documents(self, sprint_ref: str) -> list[WhyDocument]:
        return find_why_documents(self.instance, sprint_ref)

    def comment(self, sprint_ref: str, body: str, *, request_id: str) -> None:
        self._writer().comment(
            role=RESOLVER_ROLE, actor=RESOLVER_ACTOR, reference=sprint_ref, body=body, request_id=request_id
        )

    def record_po_session(self, sprint_ref: str, session_id: str, *, request_id: str) -> None:
        self._writer().set_po_session(
            role=RESOLVER_ROLE,
            actor=RESOLVER_ACTOR,
            reference=sprint_ref,
            session_id=session_id,
            request_id=request_id,
        )


__all__ = [
    "RESOLVER_ACTOR",
    "RESOLVER_ROLE",
    "BoardSprintSessions",
    "SprintRecord",
    "SprintSessions",
    "WhyDocument",
    "find_why_documents",
    "reseed_comment",
    "seed_message",
    "why_document_label",
]
