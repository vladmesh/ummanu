"""Doctor's descriptions of canonical provider probe verdicts."""

#: The probe verdicts that put a provider in front of the owner, and what each one means to them. The
#: other non-ready verdicts are transient or covered by the cross-family fallback.
PROVIDER_RED_STATES = {
    "unauthenticated": "its key is expired or its login is missing",
}
