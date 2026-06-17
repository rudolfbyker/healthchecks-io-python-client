from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, cast

from hcio_client import HealthCheck, HealthChecks
from hcio_client._json_types import JsonObject


@dataclass
class FakeResponse:
    payload: Any
    status_code: int = 200

    def json(self) -> Any:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class RequestRecorder:
    def __init__(self, *responses: FakeResponse) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = list(responses)

    def __call__(
        self,
        method: str,
        url: str,
        params: Mapping[str, str | Sequence[str]] | None = None,
        data: str | None = None,
        json: JsonObject | None = None,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "data": data,
                "json": json,
                "timeout": timeout,
                "headers": headers,
            }
        )
        if not self.responses:
            raise AssertionError("No fake response was configured for this request.")
        return self.responses.pop(0)


class HealthChecksTestCase(unittest.TestCase):
    def test_configured_slug_check_is_upserted_before_first_ping(self) -> None:
        recorder = RequestRecorder(
            FakeResponse({"uuid": "11111111-1111-1111-1111-111111111111"}),
            FakeResponse({}),
        )
        hc = HealthChecks(
            ping_base_url="https://ping.example/",
            manage_base_url="https://api.example/",
            ping_key="ping-key",
            manage_key="manage-key",
            create=True,
            request_function=cast(Any, recorder),
        )

        check = hc.check(
            slug="nightly-backup",
            desc="Runs nightly backups.",
            timeout=300,
            grace=60,
        )
        check.ping_start()

        self.assertEqual(check.uuid, "11111111-1111-1111-1111-111111111111")
        self.assertEqual(
            recorder.calls[0],
            {
                "method": "POST",
                "url": "https://api.example/checks/",
                "params": None,
                "data": None,
                "json": {
                    "slug": "nightly-backup",
                    "desc": "Runs nightly backups.",
                    "timeout": 300,
                    "grace": 60,
                    "unique": ["slug"],
                },
                "timeout": 3.0,
                "headers": {"X-Api-Key": "manage-key"},
            },
        )
        self.assertEqual(
            recorder.calls[1]["url"],
            "https://ping.example/11111111-1111-1111-1111-111111111111/start",
        )
        self.assertEqual(recorder.calls[1]["params"], {})

    def test_configured_slug_check_requires_manage_key(self) -> None:
        recorder = RequestRecorder()
        hc = HealthChecks(
            ping_key="ping-key",
            create=True,
            request_function=cast(Any, recorder),
        )

        with self.assertRaisesRegex(ValueError, "`manage_key` must be provided"):
            hc.check(
                slug="nightly-backup",
                desc="Runs nightly backups.",
            )

        self.assertEqual(recorder.calls, [])

    def test_uuid_check_still_uses_update_endpoint_for_configuration(self) -> None:
        recorder = RequestRecorder(FakeResponse({}))
        hc = HealthChecks(
            manage_base_url="https://api.example/",
            manage_key="manage-key",
            request_function=cast(Any, recorder),
        )

        check = hc.check(
            uuid="22222222-2222-2222-2222-222222222222",
            desc="Runs nightly backups.",
        )

        self.assertEqual(check.uuid, "22222222-2222-2222-2222-222222222222")
        self.assertEqual(
            recorder.calls[0],
            {
                "method": "POST",
                "url": "https://api.example/checks/22222222-2222-2222-2222-222222222222",
                "params": None,
                "data": None,
                "json": {"desc": "Runs nightly backups."},
                "timeout": 3.0,
                "headers": {"X-Api-Key": "manage-key"},
            },
        )

    def test_slug_check_without_create_updates_existing_check(self) -> None:
        recorder = RequestRecorder(
            FakeResponse(
                {
                    "checks": [
                        {
                            "uuid": "33333333-3333-3333-3333-333333333333",
                        }
                    ]
                }
            ),
            FakeResponse({}),
        )
        hc = HealthChecks(
            manage_base_url="https://api.example/",
            ping_key="ping-key",
            manage_key="manage-key",
            request_function=cast(Any, recorder),
        )

        check = hc.check(
            slug="nightly-backup",
            desc="Runs nightly backups.",
        )

        self.assertIsNone(check.uuid)
        self.assertEqual(recorder.calls[0]["method"], "GET")
        self.assertEqual(recorder.calls[0]["url"], "https://api.example/checks")
        self.assertEqual(recorder.calls[0]["params"], {"slug": "nightly-backup"})
        self.assertEqual(
            recorder.calls[1],
            {
                "method": "POST",
                "url": "https://api.example/checks/33333333-3333-3333-3333-333333333333",
                "params": None,
                "data": None,
                "json": {"desc": "Runs nightly backups."},
                "timeout": 3.0,
                "headers": {"X-Api-Key": "manage-key"},
            },
        )

    def test_per_check_manage_key_is_used_for_slug_lookup(self) -> None:
        recorder = RequestRecorder(
            FakeResponse(
                {
                    "checks": [
                        {
                            "uuid": "44444444-4444-4444-4444-444444444444",
                        }
                    ]
                }
            )
        )
        hc = HealthChecks(
            manage_base_url="https://api.example/",
            request_function=cast(Any, recorder),
        )

        check = HealthCheck(
            hc=hc,
            ping_key="ping-key",
            manage_key="per-check-key",
            slug="nightly-backup",
        )

        self.assertEqual(
            check.manage_base_url,
            "https://api.example/checks/44444444-4444-4444-4444-444444444444",
        )
        self.assertEqual(recorder.calls[0]["headers"], {"X-Api-Key": "per-check-key"})
        self.assertEqual(recorder.calls[0]["params"], {"slug": "nightly-backup"})

    def test_check_can_suppress_context_exceptions_on_exit(self) -> None:
        for suppress_exceptions_on_exit, should_raise in (
            (True, False),
            (False, True),
        ):
            with self.subTest(
                suppress_exceptions_on_exit=suppress_exceptions_on_exit,
            ):
                recorder = RequestRecorder(
                    FakeResponse({}),
                    FakeResponse({}),
                    FakeResponse({}),
                )
                hc = HealthChecks(
                    ping_base_url="https://ping.example/",
                    request_function=cast(Any, recorder),
                )

                def raise_in_context() -> None:
                    with hc.check(
                        uuid="55555555-5555-5555-5555-555555555555",
                        suppress_exceptions_on_exit=suppress_exceptions_on_exit,
                    ):
                        raise RuntimeError("boom")

                if should_raise:
                    with self.assertRaisesRegex(RuntimeError, "boom"):
                        raise_in_context()
                else:
                    raise_in_context()

                self.assertEqual(
                    [call["url"] for call in recorder.calls],
                    [
                        "https://ping.example/"
                        "55555555-5555-5555-5555-555555555555/start",
                        "https://ping.example/"
                        "55555555-5555-5555-5555-555555555555/log",
                        "https://ping.example/"
                        "55555555-5555-5555-5555-555555555555/fail",
                    ],
                )
