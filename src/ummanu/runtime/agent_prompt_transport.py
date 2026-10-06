"""Validation policy and wire form of one interactive agent prompt.

Codex gets a bracketed paste, Claude a plain body; the terminal write belongs to the head backend.
`AGENT_PROMPT_TRANSPORT_VERSION` and `TRANSPORT_POLICY` are recorded in `DeliveryEvidence`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

AGENT_PROMPT_TRANSPORT_VERSION = "agent-prompt-v2"
# Well below the 128 KiB Linux limit on one argv element, so the body can travel as a single
# argument. Framing bytes count against it.
AGENT_PROMPT_MAX_BYTES = 64 * 1024
BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"
_ALLOWED_C0 = frozenset({"\t", "\n"})
TRANSPORT_POLICY = "normalize-newlines-reject-c0-esc"


@dataclass(frozen=True)
class PreparedAgentPrompt:
    """A validated prompt and the one body form the terminal receives."""

    text: str
    adapter: str
    body: str
    framing: str
    policy: str = TRANSPORT_POLICY


@dataclass
class PromptTransportReceipt:
    """Metadata-only evidence for the body and its separate submit write."""

    transport_version: str = AGENT_PROMPT_TRANSPORT_VERSION
    adapter: str = ""
    framing: str = ""
    policy: str = TRANSPORT_POLICY
    body_write_accepted: bool = False
    body_bytes_written: int = 0
    body_write_count: int = 0
    submit_write_accepted: bool = False
    submit_bytes_written: int = 0
    submit_count: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "transport_version": self.transport_version,
            "adapter": self.adapter,
            "framing": self.framing,
            "transport_policy": self.policy,
            "body_write_accepted": self.body_write_accepted,
            "body_bytes_written": self.body_bytes_written,
            "body_write_count": self.body_write_count,
            "submit_write_accepted": self.submit_write_accepted,
            "submit_bytes_written": self.submit_bytes_written,
            "submit_count": self.submit_count,
        }


class AgentPromptTransportError(RuntimeError):
    """A rejected prompt or a failed public-terminal write with its receipt."""

    def __init__(self, reason: str, receipt: PromptTransportReceipt) -> None:
        super().__init__(reason)
        self.reason = reason
        self.receipt = receipt


def prepare_agent_prompt(text: str, *, adapter: str) -> PreparedAgentPrompt:
    """Validate prompt data before any terminal interaction and choose its wire form.


    CR/CRLF is normalised to LF first: the board's web form submits textareas with CRLF, and the
    rewrite changes no instruction nor can it forge the ESC-introduced frame. Then ESC and every
    other C0 control except tab/newline is rejected, not replaced, so the prompt never silently
    differs from the durable task document and the frame delimiter stays unforgeable.
    """
    normalized_adapter = str(adapter or "").lower()
    receipt = PromptTransportReceipt(adapter=normalized_adapter)
    if not isinstance(text, str):
        raise AgentPromptTransportError("prompt-body-invalid", receipt)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any(ord(char) < 0x20 and char not in _ALLOWED_C0 for char in text):
        raise AgentPromptTransportError("prompt-body-rejected-control", receipt)
    try:
        text.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise AgentPromptTransportError("prompt-body-invalid-unicode", receipt) from None
    if normalized_adapter == "codex":
        body = f"{BRACKETED_PASTE_START}{text}{BRACKETED_PASTE_END}"
        framing = "bracketed-paste-v1"
    else:
        body = text
        framing = "plain-v1"
    if len(body.encode("utf-8", "strict")) > AGENT_PROMPT_MAX_BYTES:
        raise AgentPromptTransportError("prompt-body-too-large", receipt)
    return PreparedAgentPrompt(text=text, adapter=normalized_adapter, body=body, framing=framing)
