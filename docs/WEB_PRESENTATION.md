# Server-rendered presentation

`ummanu.web.pages` is the public composition used by `WebApp`. It exports the existing
renderers and request context managers through explicit imports. Renderers look up their
helpers in their owning module; tests patch that owner. There is no module proxy, callback
registry, asset route or template engine.

| Owner | Responsibility | Presentation dependencies |
| --- | --- | --- |
| `page_values` | Dict-only normalization and five request ContextVars | None |
| `page_assets` | Packaged CSS/JS and the existing MAX_TABS derivation | None |
| `page_parts` | Values, status/reset timing, sprint summaries, controls, head badges | Values, assets |
| `page_shell` | Inline assets, navigation, provider bar, doctor lamp, owner bell, errors/redirects | Parts, values, assets |
| `page_overview` | Dashboard, listings, projects, doctor, history, owner events | Shell, parts, values, assets |
| `page_card` | Task, head view, origin, wait, e2e, runs and timeline | Shell, parts, values, assets |
| `page_sprint` | New/refused forms and sprint detail | Shell, parts, values, assets |
| `page_po` | PO overview, session/forms, entries, delegation and transitions | Shell, parts, values, assets |
| `pages` | Public composition | Page families, shell, values |

The public render exports are `dashboard`, `task`, `head_view`, `po_sessions_named`,
`sprints_page`, `projects_page`, `project_page`, `doctor`, `commands`, `owner_events`,
`owner_event_subject`, `sprint_form`, `sprint`, `po_login`, `po_page`, `po_session`,
`error` and `redirect`. The context exports are `limits_source`, `doctor_source`,
`bell_source`, `render_clock` and `from_post`. Private helpers and asset constants
are imported from their owners by their actual test consumers. In particular, patches
of `_head_header` and `_head_journal` belong to `page_card`, and fake-clock binding
through `pages.render_clock` controls `page_parts._render_now`.

`page_values._mapping` returns the original object for dictionaries, including subclasses,
and a fresh empty dictionary for other types. The former `_block` implementation was
removed and its calls use `_mapping`. At base
`c5e96f72a4d76a3e84bee4971df6defcda202cef`, the inventory found 34 equivalent
conditional-get expressions and one dict-or-empty local value, rather than the audit's
historical 33 conditional gets. All 35 now use `_mapping`:

| Base lines in pages.py | Actual owner | Inputs normalized |
| --- | --- | --- |
| 1344, 1346, 1369, 1376–1378, 1409, 1415–1416, 1428, 1442, 1460 | `page_parts` | Attention, budget, current task/state, checks/gate, profiles, observer/launch, waiting, carried state |
| 1534–1536 | `page_parts` | History actor, entity and result |
| 1827–1828, 1830, 2041 | `page_overview` | Sprint row current task/observer/budget and health summary |
| 2063, 2938, 2947 | `page_card` | Routing and timeline actors |
| 3615, 3617, 3621, 3631, 3683–3684, 3693–3694, 3733–3734, 3752, 3754, 3851 | `page_sprint` | Checks/gate, waiting, budget, decision/entry, card states, resume, executor |

The two old helper bodies at 2301/2389 are accounted for separately. Deliberately distinct
conditional expressions remain: 638/679/1584 require an available section and return a
None sentinel; 639 keeps the provider-document None sentinel; 682/1587 distinguish a
missing source from a refused source; 2067/2369 keep waiting-owner None; 3755 combines
type and truthiness to select a field set; 4395/4412 return a metadata source or None.
Other list filtering, truthiness, source-availability and malformed-document errors retain
their existing contracts.

Each public context wrapper calls `page_values._bind`, whose sole set/try/yield/finally/reset
body owns the ContextVar token. The renderer uses the same five variables. Source reads
remain lazy: a JSON route does not render the bar, lamp or bell. The bell still catches a
source exception and renders an unknown count; provider/doctor callable failures retain
their existing boundary behavior. Nested and interleaved copy_context fixtures exercise
independent values and restoration; no new thread/process timing fixture is needed.

The eight canonical files under `web/static` contain evaluated Python literal values,
including leading/trailing newlines and JS escapes. `importlib.resources` loads them once
into `page_assets`; `STYLE` still expands `TAB_RULES` using the real `MAX_TABS`. Missing
resources raise at loading, without a checkout fallback. Scripts/styles retain their inline
wrappers, ordering, CSP and POST refresh policy. Existing evaluated-asset assertions now
import `page_assets`. The recursive Python transport guard already covers the new modules;
the nonrecursive webproto guard covers these sibling owners. New resource guards also read
the canonical CSS/JS. No original source or security assertion was removed.

The source packet fixture covers public page families and affected helpers with fixed
request IDs, sources and clock. Optional `UMMANU_RENDER_BASE`/`UMMANU_RENDER_CAPTURE`
inputs compare an external exact-base source artifact against the candidate, recording
full packets, hashes, evaluated assets, all function moves and actual global lookups.
The old renderer is not shipped in tests. This is fixture execution on captured base bytes,
not evidence of a pristine historical whole-suite run. Normal tests use only the current
implementation. `test_web_renderer_owners` checks ownership, dependency direction, planted
dict-helper duplication, token reset and resource loading without freezing the full old AST.

The existing packaging shard's `test_web_process_coherence` additionally builds an sdist,
builds a wheel from that sdist, installs it under a scratch directory, and renders all public
packet cases from an isolated interpreter outside both source trees. It compares asset
hashes, generated tab rules, error behavior and Python/static source rules. This test runs
in CI only. The CI packaging job installs the already-declared setuptools build backend;
the product adds no dependency. The frozen coupling workflow now triggers on the page
owners and static assets, with its scanner and thresholds unchanged.

The evidence fixtures make the test addition larger than the initial estimate of a few
regressions: they include full move/global-lookup auditing, deterministic page documents
and an installed-distribution proof. Product changes remain extraction, explicit imports,
dict normalization and context binding within server-rendered presentation.

DEC-07 and the dict/context portion of DUP-W5 are this change's scope. The historical
audit counted four JS randomUUID fallbacks; this exact base has three, all preserved
byte for byte. INEF-04's product-run scan and lifecycle
reads, DUP-W4 persistence consolidation, and the rest of PR668 slot 13 remain separate.
This change claims neither a measured latency gain nor completion of the entire web DoD.
