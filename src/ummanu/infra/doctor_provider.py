"""Doctor's descriptions of canonical provider probe verdicts."""

from ummanu.head_health import PROBE_BROKEN, PROBE_TIMED_OUT

PROVIDER_RED_STATES = {
    "unauthenticated": "its key is expired or its login is missing",
    "exhausted": "its quota is spent",
    "unavailable": "its provider does not answer",
    PROBE_TIMED_OUT: "its provider gave the probe no answer in time, so claims on it are held",
    PROBE_BROKEN: "its probe cannot run, so claims on it are not gated by health",
}
