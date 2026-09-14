"""`benchpress-write/1`: one canonical JSON line per write or approval event.

The same function serializes a write in library mode (`VerifiedWrite(receipts=...)`), in the gateway's store and in
the MCP surface, so the three can be compared byte for byte under a fixed clock.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from benchpress.context import Action, Evidence, GateVerdict
from benchpress.gate import fingerprint

if TYPE_CHECKING:
    from benchpress.verified import WriteOutcome

WRITE_PROTOCOL = "benchpress-write/1"
Clock = Callable[[], str]
WriteEvent = Literal["write", "approval_requested", "approval_resolved"]


def receipt_line(
    *,
    event: WriteEvent,
    at: str,
    workspace: str,
    session: str,
    action: Action,
    verdict: GateVerdict,
    status: str | None,
    status_code: int | None,
    evidence: Sequence[Evidence],
    approval: Mapping[str, object] | None,
) -> str:
    payload: dict[str, object] = {
        "protocol": WRITE_PROTOCOL,
        "at": at,
        "event": event,
        "workspace": workspace,
        "session": session,
        "action": action.model_dump(mode="json"),
        "fingerprint": fingerprint(action),
        "verdict": verdict.model_dump(mode="json"),
        "status": status,
        "status_code": status_code,
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "approval": dict(approval) if approval is not None else None,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def write_line(
    outcome: WriteOutcome, *, at: str, workspace: str, session: str, approval: Mapping[str, object] | None = None
) -> str:
    return receipt_line(
        event="write",
        at=at,
        workspace=workspace,
        session=session,
        action=outcome.action,
        verdict=outcome.verdict,
        status=outcome.status,
        status_code=outcome.result.status_code if outcome.result is not None else None,
        evidence=outcome.evidence,
        approval=approval,
    )


class ReceiptSink(Protocol):
    async def emit(self, line: str) -> None: ...


@dataclass
class JsonlReceiptSink:
    """Appends each line to a JSONL file, creating parent directories."""

    path: Path

    async def emit(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


@dataclass
class MemoryReceiptSink:
    lines: list[str] = field(default_factory=list[str])

    async def emit(self, line: str) -> None:
        self.lines.append(line)
