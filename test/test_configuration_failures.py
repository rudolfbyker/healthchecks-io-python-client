"""
Exercise implicit configuration failures without contacting Healthchecks.
"""

import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import Mock

from requests import ConnectionError, HTTPError, ReadTimeout, Response

from hcio_client import HealthChecks


def response(*, status: int, payload: bytes) -> Response:
    """
    Construct an HTTP response for a configuration or ping request.

    Args:
        status: HTTP status code.
        payload: Response body.

    Returns:
        A response supporting status checks and JSON decoding.
    """
    result = Response()
    result.status_code = status
    result._content = payload
    return result


class ConfigurationFailureTests(unittest.TestCase):
    """
    Verify retries, fallback identity, and strict error handling.
    """

    def make_client(self, *, request: Any) -> HealthChecks:
        """
        Build a client with a short deterministic retry budget.

        Args:
            request: Fake request implementation.

        Returns:
            A client using custom endpoints and inherited identity defaults.
        """
        return HealthChecks(
            ping_base_url="https://ping.example/",
            manage_base_url="https://manage.example/",
            ping_key="default-ping-key",
            manage_key="default-manage-key",
            create=True,
            run_id="default-run",
            request_function=request,
            n_manage_attempts=3,
            n_wait_between_manage_attempts=0,
            n_ping_attempts=1,
        )

    def test_transient_setup_falls_back_without_repeating_configuration(self) -> None:
        """
        Exhaust one budget and retain overrides for both upsert and update paths.
        """
        for identity in ({}, {"uuid": "existing"}, {"create": False}):
            for failure in (
                ConnectionError("offline"),
                ReadTimeout("slow"),
                response(status=503, payload=b"unavailable"),
            ):
                with self.subTest(identity=identity, failure=failure):
                    request = Mock(
                        side_effect=failure if isinstance(failure, Exception) else None,
                        return_value=failure,
                    )
                    hc = self.make_client(request=request)
                    check = hc.check(
                        slug="backup",
                        desc="Backup",
                        ping_key="override-ping-key",
                        manage_key="override-manage-key",
                        run_id="override-run",
                        **identity,
                    )
                    self.assertEqual(request.call_count, 3)
                    self.assertIs(check.hc, hc)
                    self.assertEqual(check.uuid, identity.get("uuid"))
                    self.assertEqual(check.create, identity.get("create", True))
                    self.assertEqual(check.run_id, "override-run")
                    self.assertEqual(check.manage_key, "override-manage-key")
                    self.assertEqual(check.ping_key, "override-ping-key")
                    self.assertEqual(
                        check.ping_base_url,
                        (
                            "https://ping.example/existing"
                            if "uuid" in identity
                            else "https://ping.example/override-ping-key/backup"
                        ),
                    )

    def test_server_error_recovery_uses_uuid_and_sends_normal_pings(self) -> None:
        """
        Retry a 5xx response, then use the configured UUID and original run ID.
        """
        request = Mock(
            side_effect=[
                response(status=503, payload=b"unavailable"),
                response(status=200, payload=b'{"uuid": "created"}'),
                response(status=200, payload=b"OK"),
                response(status=200, payload=b"OK"),
            ]
        )
        work = Mock()
        with self.make_client(request=request).check(
            slug="backup", desc="Backup"
        ) as check:
            work()
        work.assert_called_once()
        self.assertEqual(check.uuid, "created")
        self.assertIsNone(check.create)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(
            request.call_args_list[-1].kwargs["url"], "https://ping.example/created"
        )
        self.assertEqual(
            request.call_args_list[-1].kwargs["params"], {"rid": "default-run"}
        )

    def test_success_after_setup_outage_depends_on_ping_connectivity(self) -> None:
        """
        Execute work during an outage and deliver success only if the network recovers.
        """
        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                delivered: list[str] = []

                def request(**kwargs: Any) -> Response:
                    """
                    Simulate management failure and optional ping recovery.

                    Args:
                        kwargs: Request arguments including its URL.

                    Returns:
                        A successful response when ping connectivity recovers.
                    """
                    url = str(kwargs["url"])
                    if url.startswith("https://manage.example/") or not recovered:
                        raise ConnectionError("offline")
                    delivered.append(url)
                    return response(status=200, payload=b"OK")

                work = Mock()
                with self.make_client(request=request).check(
                    slug="backup", desc="Backup"
                ):
                    work()
                work.assert_called_once()
                self.assertEqual(
                    delivered,
                    (
                        [
                            "https://ping.example/default-ping-key/backup/start",
                            "https://ping.example/default-ping-key/backup",
                        ]
                        if recovered
                        else []
                    ),
                )

    def test_nontransient_setup_errors_are_not_retried_or_suppressed(self) -> None:
        """
        Preserve HTTP 4xx, malformed payloads, and programming errors.
        """
        for uuid in (None, "existing"):
            for failure in (
                response(status=403, payload=b"forbidden"),
                RuntimeError("bug"),
            ):
                with self.subTest(uuid=uuid, failure=failure):
                    request = Mock(
                        side_effect=failure if isinstance(failure, Exception) else None,
                        return_value=failure,
                    )
                    with self.assertRaises((HTTPError, RuntimeError)):
                        self.make_client(request=request).check(
                            uuid=uuid, slug="backup", desc="Backup"
                        )
                    self.assertEqual(request.call_count, 1)
        for payload in (
            b"invalid JSON",
            b"[]",
            b"{}",
            b'{"uuid": 42}',
            b'{"uuid": ""}',
        ):
            with self.subTest(payload=payload):
                request = Mock(return_value=response(status=200, payload=payload))
                with self.assertRaises(ValueError):
                    self.make_client(request=request).check(
                        slug="backup", desc="Backup"
                    )
                self.assertEqual(request.call_count, 1)

    def test_explicit_management_operations_still_raise(self) -> None:
        """
        Keep explicit creation and strict updates visible to callers.
        """
        for failure in (
            ConnectionError("offline"),
            response(status=503, payload=b"unavailable"),
        ):
            for operation in ("create", "update"):
                with self.subTest(failure=failure, operation=operation):
                    request = Mock(
                        side_effect=failure if isinstance(failure, Exception) else None,
                        return_value=failure,
                    )
                    hc = self.make_client(request=request)
                    with self.assertRaises((ConnectionError, HTTPError)):
                        if operation == "create":
                            hc.create_or_update(slug="backup")
                        else:
                            hc.check(uuid="existing").manage_update(
                                desc="Backup",
                                raise_for_status=True,
                                raise_for_failed_request=True,
                            )

    def test_configuration_failure_logging_is_optional(self) -> None:
        """
        Log locally when requested, without changing fallback behavior.
        """
        hc = replace(
            self.make_client(request=Mock(side_effect=ConnectionError("offline"))),
            log_exceptions=True,
        )
        with self.assertLogs("hcio_client._health_checks", level="ERROR"):
            hc.check(slug="backup", desc="Backup")
