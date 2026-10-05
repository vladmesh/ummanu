"""Observer PO-channel admission, with a bounded sentence grammar (docs/PROTOCOLS.md).

Only direct requests/waits in active sentences are recognized, not arbitrary mentions.
Block quotations, code fences and quoted spans are evidence. Other wording must use
the explicit request marker or the resume's typed po_request; prose is not a card ref.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CREATE_CARD_HINT = "Create/link a decision or operation card for this sprint; use current_task and po_request.card for a PO wait."
# Positive continuations establish an addressee/actor. Every other word after
# the role is a modifier by default; there is no component-noun denylist.
_EN_VERB = r"(?:decide|choose|assign|approve|answer|act|resolve|confirm|review)\b"
_RU_VERB = r"(?:выбрать|выбери|выберите|выбрал|назначить|назначь|назначьте|назначил|решить|реши|решите|решил|ответить|ответь|ответьте|ответил|согласовать|согласуй|согласуйте|согласовал|подтвердить|подтверди|подтвердите|подтвердил)\b"
_EN_OBJECT = r"(?:decision|answer|action|approval)\b"
_RU_OBJECT = r"(?:решение|решения|ответ|ответа|действие|действия|согласование|согласования)\b"
_END_OR_ADDRESS = r"$|[:,]"
_EN_CONTINUATION = rf"(?:{_END_OR_ADDRESS}|\s+(?:{_EN_OBJECT}|(?:to\s+|must\s+|needs?\s+to\s+|please\s+)?{_EN_VERB})|'s\s+{_EN_OBJECT})"
_RU_CONTINUATION = rf"(?:{_END_OR_ADDRESS}|\s+(?:{_RU_OBJECT}|(?:должен\s+|нужно\s+|прошу\s+)?{_RU_VERB}))"
# Cyrillic ПО is case-sensitive: lowercase по is always a preposition.
_PO = rf"(?:po|product owner)\b(?![-\w])(?={_EN_CONTINUATION})"
_RU_PO = rf"(?-i:ПО)\b(?![-\w])(?={_RU_CONTINUATION})"
_REQUEST = re.compile(
    r"^(?:please\s+)?(?:"
    r"(?:ask|request|need|await|wait(?:ing)?\s+(?:for|on))\s+(?:(?:a|the)\s+)?" + _PO +
    r"|(?:need|await|wait(?:ing)?\s+(?:for|on))\s+(?:a\s+|the\s+)?(?:decision|action|answer|approval)\s+(?:from|by|of)\s+(?:the\s+)?" + _PO +
    r"|" + _PO + rf"(?:\s*[:,]|\s+(?:to\s+|must\s+|needs?\s+to\s+|please\s+)?{_EN_VERB})"
    r"|(?:прошу|просим|попросить|запросить|жд[её]м|жду|ожидаем|ожидаю|ожидать|ждать|дождаться)\s+(?:" + _RU_PO + r"|(?:решени[ея]|действи[ея]|ответа|согласования)\s+(?:от\s+)?" + _RU_PO + r")"
    r"|(?:нужно|нужен|нужна|требуется)\s+(?:решение|действие|ответ|согласование)\s+(?:от\s+)?" + _RU_PO +
    r"|(?:нужно|требуется)\s*,?\s*чтобы\s+" + _RU_PO +
    r"|" + _RU_PO + rf"(?:\s*[:,]|\s+(?:должен\s+|нужно\s+|прошу\s+)?{_RU_VERB}))",
    re.IGNORECASE,
)


def requests_po(text: str) -> bool:
    fenced = False
    active = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or line.startswith(">"):
            continue
        # Inline literal/quoted evidence cannot turn a mention into an instruction.
        line = re.sub(r'`[^`]*`|"[^"\n]*"|«[^»]*»|“[^”]*”', "", line)
        active.extend(re.split(r"[.!?;]\s*", line))
    for sentence in active:
        sentence = sentence.strip().lstrip("-* ")
        sentence = re.sub(r"^(?:next(?: safe step)?|следующий(?: безопасный)? шаг)\s*:\s*", "", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"^(?:we\s+|нам\s+)", "", sentence, flags=re.IGNORECASE)
        if sentence.lower().startswith("[observer:request]") or _REQUEST.match(sentence):
            return True
    return False


@dataclass(frozen=True, slots=True)
class PoRequest:
    card: str
    action: str

    @classmethod
    def from_document(cls, value: Any) -> PoRequest | None:
        if value is None:
            return None
        if (not isinstance(value, Mapping) or set(value) != {"card", "action"}
                or any(not isinstance(value.get(key), str) or not value[key].strip() for key in ("card", "action"))):
            raise ValueError("po_request requires exactly nonempty card and action strings. " + CREATE_CARD_HINT)
        return cls(value["card"].strip(), value["action"].strip())

    def to_document(self) -> dict[str, str]:
        return {"card": self.card, "action": self.action}
