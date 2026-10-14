"""Server-rendered presentation: assets."""

from __future__ import annotations

from importlib.resources import files

STYLE = files("ummanu.web").joinpath("static/style.css").read_text(encoding="utf-8")


#: Most tabs one strip may hold: the stylesheet spells the radio-to-panel rule once per position.
MAX_TABS = 6


STYLE = STYLE.replace(
    "TAB_RULES",
    "\n".join(
        f".tabs > input:nth-of-type({n}):checked ~ .tab-bar label:nth-of-type({n}) "
        "{ color: var(--ink); border-bottom-color: var(--accent); }\n"
        f".tabs > input:nth-of-type({n}):focus-visible ~ .tab-bar label:nth-of-type({n}) "
        "{ outline: 2px solid var(--accent); outline-offset: 2px; }\n"
        f".tabs > input:nth-of-type({n}):checked ~ .tab-panel:nth-of-type({n}) {{ display: block; }}"
        for n in range(1, MAX_TABS + 1)
    ),
)


_ACTIONS_SCRIPT = files("ummanu.web").joinpath("static/actions.js").read_text(encoding="utf-8")


_REFRESH_SCRIPT = files("ummanu.web").joinpath("static/refresh.js").read_text(encoding="utf-8")


_RESET_SCRIPT = files("ummanu.web").joinpath("static/reset.js").read_text(encoding="utf-8")


_TASK_SCRIPT = files("ummanu.web").joinpath("static/task.js").read_text(encoding="utf-8")


_SPRINT_FORM_SCRIPT = files("ummanu.web").joinpath("static/sprint-form.js").read_text(encoding="utf-8")


_PO_FORM_SCRIPT = files("ummanu.web").joinpath("static/po-form.js").read_text(encoding="utf-8")


_PO_SESSION_SCRIPT = files("ummanu.web").joinpath("static/po-session.js").read_text(encoding="utf-8")

