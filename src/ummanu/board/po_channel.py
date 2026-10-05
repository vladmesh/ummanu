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
# Request context establishes the actor before considering ambiguous noun phrases.
_EN_VERB = r"(?:decide|choose|assign|approve|act|resolve|confirm)\b"
_EN_AMBIGUOUS_VERB = r"(?:answer|review)\b"
_RU_VERB = r"(?:выбрать|выбери|выберите|выбрал|назначить|назначь|назначьте|назначил|решить|реши|решите|решил|ответить|ответь|ответьте|ответил|согласовать|согласуй|согласуйте|согласовал|подтвердить|подтверди|подтвердите|подтвердил)\b"
_EN_OBJECT = r"(?:decision|answer|action|approval)\b"
_RU_OBJECT = r"(?:решение|решения|ответ|ответа|действие|действия|согласование|согласования)\b"
_END_OR_ADDRESS = r"$|[:,]"
# An object followed by another bare noun is a component name, e.g. answer
# delivery. End/address or a complement preposition bounds the request object.
_EN_BOUNDARY = r"(?=$|[:,]|\s+(?:for|on|about|from|by|of|to|regarding)\b)"
_RU_BOUNDARY = r"(?=$|[:,]|\s+(?:по|на|о|об|для|от)\b)"
_EN_CONTINUATION = rf"(?:{_END_OR_ADDRESS}|\s+{_EN_OBJECT}{_EN_BOUNDARY}|'s\s+{_EN_OBJECT}{_EN_BOUNDARY}|\s+(?:to|must|needs?\s+to|please)\s+\w+)"
_RU_INFINITIVE = r"[а-яё]+(?:ть|ти|чь)\b"
_RU_CONTINUATION = rf"(?:{_END_OR_ADDRESS}|\s+{_RU_OBJECT}{_RU_BOUNDARY}|\s+(?:должен|должна|нужно|прошу)\s+\w+|\s+(?:{_RU_VERB}|{_RU_INFINITIVE}))"
# Cyrillic ПО is case-sensitive: lowercase по is always a preposition.
_PO = r"(?:po|product owner)\b(?![-\w])"
_RU_PO = r"(?-i:ПО)\b(?![-\w])"
_REQUEST = re.compile(
    r"^(?:please\s+)?(?:"
    r"(?:ask|request|need|await|wait(?:ing)?\s+(?:for|on))\s+(?:(?:a|an|the)\s+)?" + _PO + rf"(?={_EN_CONTINUATION}|\s+for\s+(?:(?:a|an|the)\s+)?{_EN_OBJECT}{_EN_BOUNDARY})" +
    r"|(?:need|await|wait(?:ing)?\s+(?:for|on))\s+(?:(?:a|an|the)\s+)?(?:decision|action|answer|approval)\s+(?:from|by|of)\s+(?:the\s+)?" + _PO +
    r"|" + _PO + rf"(?:\s*[:,]|\s+(?:to|must|needs?\s+to|please)\s+\w+|\s+{_EN_VERB}|\s+{_EN_AMBIGUOUS_VERB}(?=$|[:,]|\s+(?:the|a|an|this|that|for|on|about)\b))"
    r"|(?:прошу|просим|попросить|запросить|жд[её]м|жду|ожидаем|ожидаю|ожидать|ждать|дождаться)\s+(?:" + _RU_PO + rf"(?={_RU_CONTINUATION})|(?:решени[ея]|действи[ея]|ответа|согласования)\s+(?:от\s+)?" + _RU_PO + r")"
    r"|(?:нужно|нужен|нужна|требуется)\s+(?:решение|действие|ответ|согласование)\s+(?:от\s+)?" + _RU_PO +
    r"|(?:нужно|требуется)\s*,?\s*чтобы\s+" + _RU_PO +
    r"|" + _RU_PO + rf"(?:\s*[:,]|\s+(?:должен|должна|нужно|прошу)\s+\w+|\s+{_RU_VERB}))",
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
