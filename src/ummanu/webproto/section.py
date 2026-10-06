"""The one place a section of a snapshot is assembled, and the invariant it holds.

    A source that refused, or was never read, may not delete, shadow or fabricate an answer another
    source gave. Every section names the source that actually produced it.

* A source is a `Reading`, made once per document; :meth:`SourceSet.value` refuses the payload of a
  source that did not answer.
* :meth:`SourceSet.decide` takes ordered :class:`Rule` s. A rule runs only when every source it
  `needs` answered; the first rule that produces wins and the section carries its `answers` source.
* When no rule can produce, the section is its declared `blank`, attributed to the first consulted
  source (in precedence order) that refused. Only `narrates` fields may differ from the blank.
* If every consulted source answered and no rule produced, the rules are not total: `decide` raises.
* Provenance: only `decide` and `mark` mint a trusted section (`minted is _MINTED`). :func:`render`
  and :class:`SectionSet` (which wraps every public method, like `ProtocolBoundary`) refuse a
  section built by calling the constructor, and `render` refuses a plain mapping with a `source`.

`SectionContractError` is a `RuntimeError` kept outside `boundary.IMPLEMENTATION_FAILURES`: it is a
defect of this layer, and reporting it as `backend_unavailable` would hide it.
See docs/PROTOCOLS.md, "Sources fail apart".
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ummanu.webproto import sources

#: The four fields `sources.Source.to_json` always writes; `render` uses them to recognise a
#: hand-built section.
SOURCE_FIELDS = frozenset({"state", "reason", "observed_at", "data_age_seconds"})

#: What a refusal may still say without making a claim. Everything else is checked against the blank.
NARRATION = ("reason",)


class SectionContractError(RuntimeError):
    """A section that claims more than its source gave. A defect of this layer, never a refusal."""


@dataclass(frozen=True, slots=True)
class Reading:
    """One source of one document, read once: what it is called, whether it answered, what it gave.

    `value` is meaningful only when the source answered, and :meth:`SourceSet.value` is the only way
    a rule gets at it, so "unavailable" cannot quietly become an empty list.
    """

    key: str
    source: sources.Source
    value: Any = None

    @property
    def answered(self) -> bool:
        return self.source.state == sources.AVAILABLE


@dataclass(frozen=True, slots=True)
class Rule:
    """One way a section may be answered: from which source, needing which, by what.

    `needs` is every source whose value the rule reads, and it always contains `answers`. The rule
    receives those values, in that order, and runs only when every one of them answered. Returning
    `None` means "this rule does not settle it", and the next rule is tried.
    """

    answers: str
    needs: tuple[str, ...]
    produce: Callable[..., Mapping[str, Any] | None]

    def __post_init__(self) -> None:
        if self.answers not in self.needs:
            raise SectionContractError(
                f"a rule answering from {self.answers!r} must name it among the sources it needs"
            )


def rule(
    answers: str, produce: Callable[..., Mapping[str, Any] | None], *, needs: Sequence[str] = ()
) -> Rule:
    """`Rule`, with `needs` defaulting to the source the rule answers from."""
    return Rule(answers, tuple(needs) if needs else (answers,), produce)


#: Proof that `SourceSet.decide` or `SourceSet.mark` made a section. A private object, not a flag, so
#: a section built by calling the constructor can never carry it. Deliberately importing it is forging,
#: which this seam does not try to prevent.
_MINTED = object()


@dataclass(frozen=True, slots=True)
class Section:
    """One section: the named source that answered it, and its fields.

    The name is written to the document: two available sources look identical otherwise. A section
    not built by `decide` or `mark` is not :attr:`trusted` and is refused by `guard` and `render`.
    """

    source: sources.Source
    fields: Mapping[str, Any]
    name: str
    minted: Any = None

    @property
    def trusted(self) -> bool:
        """Whether this section was decided here, rather than assembled by a caller."""
        return self.minted is _MINTED

    def to_json(self) -> dict[str, Any]:
        return render(self)


def _minted(source: sources.Source, fields: Mapping[str, Any], name: str) -> Section:
    """The only place a trusted section is made. `decide` and `mark` are its only callers."""
    return Section(source, fields, name, _MINTED)


class SourceSet:
    """Every source of one document, read once, in the precedence they are consulted in.

    The order is the order a refusal is attributed in: when nothing could answer, the section names
    the first source of this order that was consulted and did not answer, because that is the first
    input the chain was missing.
    """

    __slots__ = ("_order", "_readings")

    def __init__(self, readings: Iterable[Reading]) -> None:
        self._readings: dict[str, Reading] = {}
        for reading in readings:
            self._readings[reading.key] = reading
        self._order: tuple[str, ...] = tuple(self._readings)

    def __contains__(self, key: object) -> bool:
        return key in self._readings

    def reading(self, key: str) -> Reading:
        try:
            return self._readings[key]
        except KeyError:
            raise SectionContractError(f"no source of this document is called {key!r}") from None

    def answered(self, key: str) -> bool:
        return self.reading(key).answered

    def source(self, key: str) -> sources.Source:
        return self.reading(key).source

    def value(self, key: str) -> Any:
        """What this source gave, and an error rather than a payload when it did not answer."""
        reading = self.reading(key)
        if not reading.answered:
            raise SectionContractError(
                f"the {key!r} source did not answer, so it has no value to read: {reading.source.reason}"
            )
        return reading.value

    def mark(self, key: str) -> Section:
        """One source's availability, said for the document as a whole and claiming nothing."""
        return _minted(self.source(key), {}, key)

    def replacing(self, key: str, value: Any) -> SourceSet:
        """This set with one source's value narrowed to one subject; its availability is unchanged."""
        self.reading(key)
        return SourceSet(
            Reading(entry.key, entry.source, value if entry.key == key else entry.value)
            for entry in self._readings.values()
        )

    def decide(
        self,
        *rules: Rule,
        blank: Mapping[str, Any],
        narrates: Sequence[str] = NARRATION,
        unresolved: Callable[[Reading], Mapping[str, Any]] | None = None,
    ) -> Section:
        """The section these rules decide, attributed to the source that decided it.

        `blank` is what this section says when nothing could answer: its claim fields, at the values
        that claim nothing. `narrates` names the fields a refusal may still fill -- the reason, and
        the identifiers the section is about -- and every other field is checked against `blank`.
        """
        consulted: list[str] = []
        for one in rules:
            for key in one.needs:
                self.reading(key)
                if key not in consulted:
                    consulted.append(key)
        for one in rules:
            if not all(self.answered(key) for key in one.needs):
                continue
            produced = one.produce(*(self.value(key) for key in one.needs))
            if produced is None:
                continue
            return _minted(self.source(one.answers), self._checked(produced, blank), one.answers)
        refused = next(
            (key for key in self._order if key in consulted and not self.answered(key)), None
        )
        if refused is None:
            raise SectionContractError(
                "every source this section consults answered and no rule settled it: "
                f"the rules over {consulted} are not total"
            )
        reading = self.reading(refused)
        fields = self._checked(unresolved(reading) if unresolved else dict(blank), blank)
        for name, value in blank.items():
            if name not in narrates and fields[name] != value:
                raise SectionContractError(
                    f"{refused!r} did not answer, so this section may not claim {name}={fields[name]!r}"
                )
        return _minted(reading.source, fields, refused)

    @staticmethod
    def _checked(produced: Mapping[str, Any], blank: Mapping[str, Any]) -> dict[str, Any]:
        """The same fields in every branch: a branch that forgets one is a defect, not an omission."""
        if set(produced) != set(blank):
            raise SectionContractError(
                f"a section answered with fields {sorted(produced)} where it declares {sorted(blank)}"
            )
        return dict(produced)


def render(node: Any) -> Any:
    """The assembled document as JSON; refuses an untrusted `Section` or a hand-built section mapping."""
    if isinstance(node, Section):
        if not node.trusted:
            raise SectionContractError(
                f"the {node.name!r} section was not decided by this seam, so it may not be published; "
                "a document's sections are built by SourceSet.decide"
            )
        return {
            "source": {**node.source.to_json(), "name": node.name},
            **{key: render(value) for key, value in node.fields.items()},
        }
    if isinstance(node, Mapping):
        carried = node.get("source")
        if isinstance(carried, Mapping) and SOURCE_FIELDS <= set(carried):
            raise SectionContractError(
                f"a section carrying {sorted(node)} was assembled outside this seam; "
                "a document's sections are built by SourceSet.decide"
            )
        return {key: render(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [render(value) for value in node]
    return node


#: Set on a wrapped builder, so tests can detect guarding and wrapping twice is a no-op.
GUARDED = "__webproto_section__"


def guard(function: Callable[..., Any]) -> Callable[..., Any]:
    """`function`, required to answer with a `Section` and nothing else."""
    if getattr(function, GUARDED, False):
        return function

    @functools.wraps(function)
    def builder(*args: Any, **kwargs: Any) -> Any:
        produced = function(*args, **kwargs)
        if not isinstance(produced, Section):
            raise SectionContractError(
                f"{function.__name__!r} is a section and must answer with one, not "
                f"{type(produced).__name__}"
            )
        if not produced.trusted:
            raise SectionContractError(
                f"{function.__name__!r} assembled its section itself; a section is decided by "
                "SourceSet.decide, which is what holds the invariant over it"
            )
        return produced

    setattr(builder, GUARDED, True)
    return builder


def sections(cls: type) -> tuple[str, ...]:
    """The sections a set defines, in definition order. The same predicate the wrapping uses."""
    return tuple(
        name
        for name, attribute in vars(cls).items()
        if not name.startswith("_") and inspect.isfunction(attribute)
    )


class SectionSet:
    """A class whose public methods each assemble one section of a document.

    Every public method is wrapped at class creation (see :func:`guard`) and must return a trusted
    section, so a new section is covered by being one. Private helpers are untouched.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name in sections(cls):
            setattr(cls, name, guard(vars(cls)[name]))
