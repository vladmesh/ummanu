"""The owner spends one Codex reset credit from the web (secretary-1776).

Hermetic: no network and no store. The provider is a recording `post_json` and `fetch_json` over a
temporary home; the audit is an in-memory stand-in holding `SqlTaskAudit`'s rules for a generic
record (a staged generic record is replaced by the next stage, a committed one owns its id). The
same record against a real `requests` table is `tests/test_web_codex_reset_audit.py`.
"""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.error import HTTPError, URLError

from tests.web_fakes import Recording
from ummanu.config import validate
from ummanu.tasks import TaskError
from ummanu.web import pages
from ummanu.web.app import WebApp
from ummanu.web.provider_usage import (
    CLAUDE_USAGE_URL,
    CODEX_RESET_CONSUME_URL,
    CODEX_RESET_CREDITS_URL,
    CODEX_RESET_TIMEOUT,
    CODEX_USAGE_URL,
    ProviderUsageLayer,
)
from ummanu.webproto.command_reads import CommandReadLayer
from ummanu.webproto.provider_ops import (
    CODEX_RESET_KIND,
    NO_CREDIT,
    ProviderOperationLayer,
)

NOW = 1_800_000_000.0
#: A token shaped like none a provider issues, so finding it anywhere is finding a leak.
TOKEN = "tok-FAKE-9c1e7a52-never-print-me"
ACCOUNT = "acct-FAKE-4411"


class MemoryAudit:
    """`SqlTaskAudit`'s claim rules for generic records, over two dictionaries."""

    def __init__(self) -> None:
        self.committed: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}

    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        return self.committed.get(request_id)

    def pending_event(self, request_id: str) -> dict[str, Any] | None:
        return self.pending.get(request_id)

    def stage(self, request_id: str, event: dict[str, Any]) -> None:
        if request_id not in self.committed:
            self.pending[request_id] = json.loads(json.dumps(event))

    def append(self, request_id: str, event: dict[str, Any]) -> str:
        existing = self.committed.get(request_id)
        if existing is not None:
            if existing != event:
                raise TaskError("validation", "request id belongs to another operation or payload", 2)
        else:
            staged = self.pending.get(request_id)
            if staged is not None and staged != event:
                raise TaskError("validation", "request id belongs to another operation or payload", 2)
            self.committed[request_id] = json.loads(json.dumps(event))
            self.pending.pop(request_id, None)
        return str(event["event_id"])

    def events_page(self, *, end: int | None, limit: int) -> tuple[int, list[dict[str, Any]]]:
        records = list(self.committed.values())
        stop = len(records) if end is None else end
        return len(records), records[max(0, stop - limit) : stop]


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class Provider:
    """The Codex backend as the layer sees it: a usage document, a credits list and a consume answer."""

    def __init__(self, *, available: Any = 1, applicable: Any = 1) -> None:
        self.credits: dict[str, Any] | None = {
            "available_count": available,
            "applicable_available_count": applicable,
        }
        self.answer: Any = {"code": "reset"}
        self.fetched: list[str] = []
        self.posted: list[tuple[str, dict[str, str], dict[str, Any], float]] = []
        self.usage_fails = False

    def fetch(self, url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        self.fetched.append(url)
        if url == CODEX_RESET_CREDITS_URL:
            return {"credits": [{"status": "available", "expires_at": "2026-10-22T20:24:55Z"}]}
        if url == CLAUDE_USAGE_URL:
            return {"five_hour": {"utilization": 10, "resets_at": NOW + 100}}
        if self.usage_fails:
            raise TimeoutError
        raw: dict[str, Any] = {
            "rate_limit": {
                "primary_window": {"used_percent": 100, "window_minutes": 300, "reset_at": NOW + 300}
            }
        }
        if self.credits is not None:
            raw["rate_limit_reset_credits"] = self.credits
        return raw

    def post(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> Any:
        self.posted.append((url, headers, body, timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class ResetFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        write(self.home / ".codex/auth.json", {"tokens": {"access_token": TOKEN, "account_id": ACCOUNT}})
        self.provider = Provider()
        self.clock = NOW
        self.usage = ProviderUsageLayer(
            home=self.home,
            fetch_json=self.provider.fetch,
            post_json=self.provider.post,
            now=lambda: self.clock,
        )
        self.audit = MemoryAudit()
        for seam in (
            "ummanu.webproto.provider_ops.task_audit_for",
            "ummanu.webproto.command_reads.task_audit_for",
        ):
            self.enterContext(mock.patch(seam, lambda *_args, **_kwargs: self.audit))
        self.layer = ProviderOperationLayer(
            self.home / "instance", usage=self.usage, board_client=object(), clock=lambda: self.clock
        )

    def press(self, request_id: str = "web-codex-reset-1") -> dict[str, Any]:
        return self.layer.codex_reset_limit(request_id=request_id, actor="web")

    def consumes(self) -> int:
        return len(self.provider.posted)

    def assertNoToken(self, *outputs: Any) -> None:
        for output in outputs:
            text = output if isinstance(output, str) else json.dumps(output, default=str)
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(ACCOUNT, text)


# -- the provider call ---------------------------------------------------------------------------


class ConsumeCallTests(ResetFixture):
    def test_the_request_is_the_references_one(self) -> None:
        self.assertEqual(self.usage.consume_codex_reset("rid-1"), ("reset", None))
        [(url, headers, body, timeout)] = self.provider.posted
        self.assertEqual(url, CODEX_RESET_CONSUME_URL)
        self.assertEqual(
            headers,
            {
                "Authorization": f"Bearer {TOKEN}",
                "ChatGPT-Account-Id": ACCOUNT,
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(body, {"redeem_request_id": "rid-1"})
        self.assertEqual(timeout, CODEX_RESET_TIMEOUT)
        self.assertEqual(CODEX_RESET_TIMEOUT, 30.0)

    def test_each_known_code_is_its_own_outcome(self) -> None:
        for code in ("reset", "nothing_to_reset", "no_credit", "already_redeemed"):
            with self.subTest(code=code):
                self.provider.answer = {"code": code}
                self.assertEqual(self.usage.consume_codex_reset("rid"), (code, None))

    def test_every_failure_is_an_error_with_a_reason_and_never_raises(self) -> None:
        cases: list[tuple[Any, str]] = [
            ({"code": "exploded"}, "unknown code: 'exploded'"),
            ({"code": None}, "unknown code: 'NoneType'"),
            ({}, "unknown code"),
            (["reset"], "not a JSON object"),
            ("reset", "not a JSON object"),
            (json.JSONDecodeError("x", "doc", 0), "not JSON"),
            (HTTPError(CODEX_RESET_CONSUME_URL, 500, "boom", {}, BytesIO(b"")), "HTTP 500"),
            (TimeoutError(), "within 30 s"),
            (socket.timeout(), "within 30 s"),
            (URLError(TimeoutError()), "within 30 s"),
            (URLError(ConnectionRefusedError()), "could not be reached (ConnectionRefusedError)"),
            (ConnectionResetError(), "could not be reached (ConnectionResetError)"),
            (RuntimeError(TOKEN), "failed (RuntimeError)"),
            ({"code": f"leak {TOKEN}"}, "[redacted]"),
        ]
        for answer, reason in cases:
            with self.subTest(answer=repr(answer)[:60]):
                self.provider.answer = answer
                outcome, said = self.usage.consume_codex_reset("rid")
                self.assertEqual(outcome, "error")
                self.assertIn(reason, said or "")
                self.assertNoToken(said)

    def test_no_login_is_an_error_and_sends_nothing(self) -> None:
        (self.home / ".codex/auth.json").unlink()
        self.assertEqual(self.usage.consume_codex_reset("rid"), ("error", "Codex login is unavailable"))
        self.assertEqual(self.consumes(), 0)


class CacheInvalidationTests(ResetFixture):
    def test_invalidate_makes_the_next_snapshot_read_fresh(self) -> None:
        first = self.usage.usage_snapshot()
        self.assertIs(self.usage.usage_snapshot(), first)
        self.usage.invalidate()
        self.assertIsNot(self.usage.usage_snapshot(), first)

    def test_the_live_read_passes_the_cache_and_leaves_it_alone(self) -> None:
        cached = self.usage.usage_snapshot()
        before = self.provider.fetched.count(CODEX_USAGE_URL)
        live = self.usage.codex_live()
        self.assertEqual(self.provider.fetched.count(CODEX_USAGE_URL), before + 1)
        self.assertEqual(live["reset_credits"]["applicable"], 1)
        self.assertIs(self.usage.usage_snapshot(), cached)

    def test_only_a_reset_outcome_invalidates(self) -> None:
        for code in ("reset", "nothing_to_reset", "no_credit", "already_redeemed", "bogus"):
            with self.subTest(code=code):
                self.provider.answer = {"code": code}
                with mock.patch.object(self.usage, "invalidate", wraps=self.usage.invalidate) as invalidate:
                    self.press(f"rid-{code}")
                self.assertEqual(invalidate.call_count, 1 if code == "reset" else 0)
        with mock.patch.object(self.usage, "invalidate") as invalidate:
            self.provider.credits = {"available_count": 1, "applicable_available_count": 0}
            self.press("rid-refused")
        invalidate.assert_not_called()

    def test_after_a_reset_the_next_render_reads_fresh(self) -> None:
        cached = self.usage.usage_snapshot()
        self.press()
        self.assertIsNot(self.usage.usage_snapshot(), cached)


# -- the operation: idempotency, precheck, record -----------------------------------------------


class ResetOperationTests(ResetFixture):
    def test_a_repeat_with_the_same_id_makes_one_consume_call(self) -> None:
        first = self.press()
        second = self.press()
        self.assertEqual(self.consumes(), 1)
        self.assertEqual(first["outcome"], "reset")
        self.assertFalse(first["replayed"])
        self.assertEqual(second["outcome"], "reset")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["event_id"], first["event_id"])

    def test_a_repeat_reads_the_record_before_the_provider(self) -> None:
        self.press()
        fetched = len(self.provider.fetched)
        self.press()
        self.assertEqual(len(self.provider.fetched), fetched, "a repeat asks the provider nothing")

    def test_the_client_request_id_is_the_redeem_request_id(self) -> None:
        self.press("web-codex-reset-abc")
        self.assertEqual(self.provider.posted[0][2], {"redeem_request_id": "web-codex-reset-abc"})

    def test_applicable_zero_or_unknown_still_sends_the_consume_and_records_the_providers_answer(
        self,
    ) -> None:
        """Whether a reset applies is the provider's call, made on the consume; the layer does not guess."""
        for applicable in (0, None, "garbage"):
            with self.subTest(applicable=applicable):
                self.provider.credits = {"available_count": 1, "applicable_available_count": applicable}
                self.provider.answer = {"code": "nothing_to_reset"}
                answer = self.press(f"rid-{applicable}")
                self.assertEqual(answer["outcome"], "nothing_to_reset")
                record = self.audit.committed[f"rid-{applicable}"]
                self.assertEqual((record["kind"], record["outcome"]), (CODEX_RESET_KIND, "nothing_to_reset"))
        self.assertEqual(self.consumes(), 3)

    def test_no_credit_or_no_live_reading_is_refused_without_a_consume(self) -> None:
        cases = {
            "zero": lambda: setattr(
                self.provider, "credits", {"available_count": 0, "applicable_available_count": 0}
            ),
            "absent": lambda: setattr(self.provider, "credits", None),
            "fallback": lambda: setattr(self.provider, "usage_fails", True),
        }
        for name, arrange in cases.items():
            with self.subTest(case=name):
                arrange()
                answer = self.press(f"rid-{name}")
                self.assertEqual((answer["outcome"], answer["reason"]), ("refused", NO_CREDIT))
                self.assertIn(f"rid-{name}", self.audit.committed)
        self.assertEqual(self.consumes(), 0)

    def test_the_precheck_reads_live_past_the_cache(self) -> None:
        self.usage.usage_snapshot()  # the bar's cached reading says one credit
        self.provider.credits = {"available_count": 0, "applicable_available_count": 0}
        self.assertEqual(self.press()["outcome"], "refused")
        self.assertEqual(self.consumes(), 0)

    def test_each_consume_outcome_is_recorded_and_errors_do_not_crash(self) -> None:
        cases: list[tuple[Any, str]] = [
            ({"code": "reset"}, "reset"),
            ({"code": "nothing_to_reset"}, "nothing_to_reset"),
            ({"code": "no_credit"}, "no_credit"),
            ({"code": "already_redeemed"}, "already_redeemed"),
            ({"code": "mystery"}, "error"),
            (json.JSONDecodeError("x", "<html>", 0), "error"),
            (HTTPError(CODEX_RESET_CONSUME_URL, 500, "boom", {}, BytesIO(b"")), "error"),
            (TimeoutError(), "error"),
        ]
        for index, (answer, outcome) in enumerate(cases):
            with self.subTest(outcome=outcome, index=index):
                self.provider.answer = answer
                request_id = f"rid-{index}"
                document = self.press(request_id)
                self.assertEqual(document["outcome"], outcome)
                record = self.audit.committed[request_id]
                self.assertEqual(record["outcome"], outcome)
                self.assertEqual(record["request_id"], request_id)
                self.assertNotIn(request_id, self.audit.pending)
                if outcome == "error":
                    self.assertTrue(record["reason"])
                self.assertNoToken(document, record)

    def test_every_answer_validates_against_the_published_schema(self) -> None:
        """A sent press, its replay, a refusal and a provider error: each is a `web-provider` document."""
        answers = [self.press("rid-sent"), self.press("rid-sent")]
        self.provider.answer = {"code": "mystery"}
        answers.append(self.press("rid-error"))
        self.provider.credits = {"available_count": 0, "applicable_available_count": 0}
        answers.append(self.press("rid-refused"))
        self.assertEqual([a["outcome"] for a in answers], ["reset", "reset", "error", "refused"])
        for answer in answers:
            with self.subTest(outcome=answer["outcome"], replayed=answer["replayed"]):
                self.assertEqual(validate(answer, "web-provider", answer["kind"]), [])

    def test_the_record_names_the_actor_the_action_the_id_and_the_outcome(self) -> None:
        self.press("rid-7")
        record = self.audit.committed["rid-7"]
        self.assertEqual(record["actor"], {"role": "po", "id": "web"})
        self.assertEqual(record["kind"], CODEX_RESET_KIND)
        self.assertEqual(record["request_id"], "rid-7")
        self.assertEqual(record["outcome"], "reset")
        self.assertEqual(record["backend"]["revision"], "not_written")
        self.assertEqual(record["payload"]["credits"], {"available": 1, "applicable": 1})

    def test_a_staged_press_left_behind_is_finished_under_the_same_id(self) -> None:
        self.press("rid-crash")
        staged = dict(self.audit.committed.pop("rid-crash"), outcome="unknown")
        self.audit.pending["rid-crash"] = staged
        self.provider.answer = {"code": "already_redeemed"}
        self.assertEqual(self.press("rid-crash")["outcome"], "already_redeemed")
        self.assertEqual(self.audit.committed["rid-crash"]["outcome"], "already_redeemed")

    def test_an_empty_id_is_refused_before_anything(self) -> None:
        for request_id in ("", "  "):
            with self.subTest(request_id=request_id), self.assertRaises(Exception) as caught:
                self.press(request_id)
            self.assertEqual(getattr(caught.exception, "code", None), "validation")
        self.assertEqual((self.consumes(), self.provider.fetched), (0, []))

    def test_an_id_another_operation_owns_is_refused(self) -> None:
        self.audit.committed["rid-other"] = {"kind": "commented", "event_id": "e", "request_id": "rid-other"}
        with self.assertRaises(Exception) as caught:
            self.press("rid-other")
        self.assertEqual(getattr(caught.exception, "code", None), "owner_conflict")
        self.assertEqual(self.consumes(), 0)

    def test_command_request_and_history_find_the_record(self) -> None:
        self.press("rid-history")
        reads = CommandReadLayer(self.home / "instance", data_dir=self.home / "data", board_client=object())
        # No instance config here: with the data directory given, only the audit decides these reads.
        found = reads.command_request("rid-history")
        history = reads.command_history()
        operation = found["operation"]
        self.assertEqual(operation["state"], "committed")
        self.assertEqual(operation["action"], CODEX_RESET_KIND)
        self.assertEqual(operation["actor"], {"id": "web", "role": "po"})
        self.assertEqual(operation["result"]["outcome"], "reset")
        [row] = history["commands"]["items"]
        self.assertEqual((row["request_id"], row["action"]), ("rid-history", CODEX_RESET_KIND))
        self.assertNoToken(found, history)


# -- the route -----------------------------------------------------------------------------------


class RouteTests(ResetFixture):
    def app(self, provider_ops: Any = ...) -> WebApp:
        layers = [Recording() for _ in range(8)]
        return WebApp(
            *layers,
            provider_usage=self.usage,
            provider_ops=self.layer if provider_ops is ... else provider_ops,
        )

    def post(self, body: bytes, **kwargs: Any) -> tuple[int, dict[str, Any], str]:
        response = self.app(**kwargs).handle("POST", "/api/providers/codex/reset-limit", body=body)
        text = response.body.decode("utf-8")
        return response.status, json.loads(text), text

    def test_json_and_a_repeat_answer_the_recorded_outcome_once(self) -> None:
        status, first, text = self.post(b'{"request_id": "rid-http"}')
        self.assertEqual((status, first["outcome"], first["request_id"]), (200, "reset", "rid-http"))
        status, second, _ = self.post(b'{"request_id": "rid-http"}')
        self.assertEqual((status, second["replayed"]), (200, True))
        self.assertEqual(self.consumes(), 1)
        self.assertNoToken(text)

    def test_a_missing_or_empty_id_is_refused_with_a_reason(self) -> None:
        for body in (b"{}", b'{"request_id": ""}', b'{"request_id": "  "}'):
            with self.subTest(body=body):
                status, answer, _ = self.post(body)
                self.assertEqual(status, 400)
                self.assertEqual(answer["error"]["code"], "validation")
                self.assertTrue(answer["error"]["message"])
        self.assertEqual(self.consumes(), 0)

    def test_the_route_takes_exactly_the_request_id(self) -> None:
        status, answer, _ = self.post(b'{"request_id": "r", "count": "2"}')
        self.assertEqual(status, 400)
        self.assertIn("count", answer["error"]["message"])
        self.assertEqual(self.consumes(), 0)

    def test_a_cross_origin_post_never_reaches_the_operation(self) -> None:
        response = self.app().handle(
            "POST",
            "/api/providers/codex/reset-limit",
            body=b'{"request_id": "r"}',
            headers={"Origin": "https://evil.example", "Host": "127.0.0.1:8765"},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.consumes(), 0)

    def test_a_process_without_the_layer_says_so(self) -> None:
        status, answer, _ = self.post(b'{"request_id": "r"}', provider_ops=None)
        self.assertEqual(status, 503)
        self.assertIn("provider operation layer", answer["error"]["message"])


# -- the button ----------------------------------------------------------------------------------


def codex_place(credits: Any, *, carry: bool = True, age: float = 0.0) -> str:
    codex = {
        "id": "codex",
        "label": "Codex",
        "status": "available",
        "reason": None,
        "observed_at": "2026-09-20T12:00:00Z",
        "age_seconds": age,
        "windows": [{"name": "5-hour", "window_minutes": 300, "remaining_percent": 0.0, "resets_at": None}],
    }
    if carry:
        codex["reset_credits"] = credits
    section = {"available": True, "reason": None, "document": {"providers": [codex]}}
    return pages._limits_bar_of(section)


class ButtonTests(unittest.TestCase):
    def test_enabled_whenever_a_credit_is_available_and_it_names_what_it_spends(self) -> None:
        for applicable in (1, 0, None):
            with self.subTest(applicable=applicable):
                bar = codex_place({"available": 2, "applicable": applicable, "next_expires_at": None})
                self.assertIn(
                    '<button type="button" class="bar-reset" data-codex-reset '
                    'data-confirm="Spend 1 of 2 Codex reset credits?"',
                    bar,
                )
                self.assertNotIn("disabled", bar)
                self.assertNotIn("exhausted", bar)

    def test_the_button_sits_inside_the_folded_credits_with_the_applicability_line(self) -> None:
        bar = codex_place({"available": 1, "applicable": 0, "next_expires_at": None})
        details = bar[bar.index('<details class="credits">') : bar.index("</details>")]
        self.assertIn('class="bar-reset"', details)
        self.assertIn(pages.RESET_NOT_APPLICABLE, details)
        self.assertLess(details.index("pop-line warn"), details.index('class="bar-reset"'))
        bar = codex_place({"available": 1, "applicable": 1, "next_expires_at": None})
        self.assertIn(pages.RESET_APPLICABLE, bar)
        self.assertNotIn(pages.RESET_NOT_APPLICABLE, bar)

    def test_absent_with_no_available_credit_or_on_a_fallback_reading(self) -> None:
        for credits, carry, age in (
            ({"available": 0, "applicable": 0, "next_expires_at": None}, True, 0.0),
            ({"available": 0, "applicable": 3, "next_expires_at": None}, True, 0.0),
            (None, False, 1200.0),  # a rollout fallback carries no credits at all
            ("garbage", True, 0.0),
        ):
            with self.subTest(credits=credits, carry=carry):
                self.assertNotIn("bar-reset", codex_place(credits, carry=carry, age=age))

    def test_the_bar_stays_one_line(self) -> None:
        bar = codex_place({"available": 1, "applicable": 1, "next_expires_at": None})
        self.assertEqual(bar.count('<div class="row">'), 1)
        self.assertNotIn("<br", bar)
        self.assertIn("white-space: nowrap", pages.STYLE)

    def test_every_page_carries_the_click_script_and_it_confirms_before_it_posts(self) -> None:
        script = pages._RESET_SCRIPT
        self.assertIn(script, pages._page("t", "<p>x</p>"))
        with pages.from_post(True):
            self.assertIn(script, pages._page("t", "<p>x</p>"))
        self.assertLess(script.index("window.confirm(button.dataset.confirm)"), script.index("fetch("))
        self.assertIn("/api/providers/codex/reset-limit", script)
        self.assertIn("ummanuReloadWhenIdle", script)
        self.assertIn("window.ummanuReloadWhenIdle", pages._REFRESH_SCRIPT)
        self.assertNotIn("location.reload", script, "the reset reloads only through the page's rule")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
