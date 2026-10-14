"""Pure Linux per-process stat tokenization, with no process or liveness policy.

Offsets address fields after comm (kernel field 3 onward). Readers that historically
discarded the separator after comm retain that spelling, including malformed input;
readers that split immediately after comm use ``skip_separator=False``.
"""

STATE = 0
PPID = 1
PGRP = 2
UTIME = 11
STIME = 12
CUTIME = 13
CSTIME = 14
STARTTIME = 19


def proc_stat_fields(text: str, *, skip_separator: bool = True) -> list[str]:
    """Split after the final comm close; missing close raises ValueError.

    Do not validate field count, state, PID, or numbers here. Some readers need
    only PPID, others deliberately accept a raw start token. ``skip_separator``
    preserves the older readers' one-character separator consumption even when
    malformed text omits whitespace after the close.
    """
    close = text.rindex(")")
    return text[close + 1 + skip_separator :].split()
