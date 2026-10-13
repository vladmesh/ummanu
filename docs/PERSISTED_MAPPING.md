# Dispatcher mapping persistence

`dispatch.state._PersistedMapping` owns construction, shallow dict coercion,
`from_value` and `to_json` for the eight public `Persisted*` wrappers. The base has
empty slots. Each wrapper selects its domain type through a static method, so
typed inputs retain ordinary `isinstance` behavior without binding a domain
constructor as an instance method. `from_value` remains a classmethod and uses
`cls`; its `Self` annotation includes subclasses.

Construction serializes typed inputs, copies dict inputs at the top level, and
uses an empty payload for other inputs, including non-dict mappings. It initializes
the stored dict before invoking the cache hook. `to_json` returns a new top-level
dict of the current mapping; nested values remain shared. Neither path normalizes
historical schemas, sorts keys, drops unknown keys or adds absent fields.

| Wrapper suffix | Typed input serializer | View / slot | Parse policy |
| --- | --- | --- | --- |
| GateReceipt | `as_dict` | cached `receipt` / `_receipt` | Lazy domain import; `accept(payload, current_sha=str(payload.get("validated_sha") or ""))`; no exception catch |
| GatePrAuthorship | `to_json` | cached `authorship` / `_authorship` | `from_json`; no exception catch |
| GatePublishedRef | `to_json` | cached `published_ref` / `_published_ref` | `from_json`; no exception catch |
| DeliveryEvidence | `to_json` | cached `evidence` / `_evidence` | `from_json`; no exception catch |
| LaunchIntent | `to_json` | live `intent`; no slots | `from_json(self)` on every access, including empty mappings |
| HeadlessRecoveryEpisode | `to_json` | live `episode`; no slots | `from_json(self)` on every access, including empty mappings |
| HeadRun | `to_json` | cached `run` / `_run` | `from_json`; catches KeyError, RuntimeError, TypeError, ValueError |
| RoutingHeadSnapshot | `to_json` | cached `snapshot` / `_snapshot` | `from_json`; catches KeyError, TypeError, ValueError; RuntimeError propagates |

Cached views parse only a nonempty raw payload, once at construction. Typed inputs
retain their object identity and bypass parsing. Subsequent mapping edits change
the persisted JSON but leave cached views intact. Launch recovery and headless
recovery still mutate their mappings in place, so their properties parse each
access and their wrappers retain instance attributes. Parser globals remain in
their original owning modules; no patch seam was moved.

`DispatcherRecord.__setattr__` and `from_json` retain the existing eleven-field
normalization, explicit `from_value` calls, evaluation order and unsupported legacy
continuation refusal. Record serializers and conditional keys are unchanged.
No dispatcher lifecycle, recovery policy, authority, schema or migration changed.

The native unit module `tests.test_persisted_mapping` checks shared method ownership,
all eight input contracts, JSON bytes and key order, shallow aliases, mapping
mutation, slot behavior, subclass construction, cached identity, live faults,
parser argument/error policies, all eleven record fields and restart parse order.
The original launch, routing and delivery integration tests remain in their CI
shards with their assertions intact.

`tests/support/persisted_mapping_goldens.json` was captured from the production
serializers at base `8abf2a9c5ee25ac15d8ca93978ff95405434a51c`, before consolidation,
using the deterministic support values. That fixture capture is not itself an
independent baseline suite. The new module was then executed through the declared
native subset runner against unchanged base production source: 13 behavioral
tests passed and the new shared-owner guard failed. The initial fixture-development
run also exposed an incorrectly typed `review_provider_hold` test input; it was
corrected to the released string contract before that baseline run. Candidate
results and exact commands are recorded in the worker report.

Only state.py changes product code. Its product import targets are unchanged,
including the lazy receipt edge; the added `Self` import is standard library.
The historical 406-product-module / 407-node / SCC-25 / config-10 figures are
contextual baseline metrics, not a new candidate graph measurement. Candidate
all-four graph evidence comes from CI; the final-main coupling check remains a
dispatcher release obligation. This cut closes DUP-D3 and the explicit shared
Persisted base requirement, not the wider DEC-04 or remaining sprint DoD.
