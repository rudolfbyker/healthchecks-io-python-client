import unittest
from typing import Mapping, Sequence

from requests import Response

from hcio_client import HealthCheck, HealthChecks
from hcio_client._health_checks import RequestFunction
from hcio_client._json_types import JsonObject

RequestRecord = dict[str, object]


def make_request_recorder() -> tuple[list[RequestRecord], RequestFunction]:
    requests: list[RequestRecord] = []

    def request_function(
        method: str,
        url: str,
        params: Mapping[str, str | Sequence[str]] | None = None,
        data: str | None = None,
        json: JsonObject | None = None,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        response = Response()
        response.status_code = 200
        requests.append(
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
        return response

    return requests, request_function


class HealthCheckExitTests(unittest.TestCase):
    def make_health_checks(self) -> tuple[HealthChecks, list[RequestRecord]]:
        requests, request_function = make_request_recorder()
        hc = HealthChecks(
            ping_base_url="https://example.test/ping/",
            request_function=request_function,
            n_ping_attempts=1,
            n_wait_between_ping_attempts=0,
        )
        return hc, requests

    def make_check(
        self,
        *,
        suppress_exceptions_on_exit: bool = False,
        suppress_success_ping_on_exit: bool = False,
        suppress_failure_ping_on_exit: bool = False,
    ) -> tuple[HealthCheck, list[RequestRecord]]:
        hc, requests = self.make_health_checks()
        return (
            hc.check(
                uuid="abc",
                suppress_exceptions_on_exit=suppress_exceptions_on_exit,
                suppress_success_ping_on_exit=suppress_success_ping_on_exit,
                suppress_failure_ping_on_exit=suppress_failure_ping_on_exit,
            ),
            requests,
        )

    @staticmethod
    def request_urls(requests: list[RequestRecord]) -> list[str]:
        return [str(request["url"]) for request in requests]

    def test_successful_context_sends_start_and_success_pings(self) -> None:
        check, requests = self.make_check()

        with check:
            pass

        self.assertEqual(
            self.request_urls(requests),
            [
                "https://example.test/ping/abc/start",
                "https://example.test/ping/abc",
            ],
        )

    def test_success_ping_can_be_suppressed_on_exit(self) -> None:
        check, requests = self.make_check(suppress_success_ping_on_exit=True)

        with check:
            pass

        self.assertEqual(
            self.request_urls(requests),
            ["https://example.test/ping/abc/start"],
        )

    def test_failed_context_sends_start_exception_log_and_failure_pings(self) -> None:
        check, requests = self.make_check()

        with self.assertRaises(RuntimeError):
            with check:
                raise RuntimeError("boom")

        self.assertEqual(
            self.request_urls(requests),
            [
                "https://example.test/ping/abc/start",
                "https://example.test/ping/abc/log",
                "https://example.test/ping/abc/fail",
            ],
        )

    def test_failure_ping_can_be_suppressed_on_exit(self) -> None:
        check, requests = self.make_check(suppress_failure_ping_on_exit=True)

        with self.assertRaises(RuntimeError):
            with check:
                raise RuntimeError("boom")

        self.assertEqual(
            self.request_urls(requests),
            [
                "https://example.test/ping/abc/start",
                "https://example.test/ping/abc/log",
            ],
        )

    def test_exception_can_be_suppressed_on_exit(self) -> None:
        check, requests = self.make_check(suppress_exceptions_on_exit=True)

        with check:
            raise RuntimeError("boom")

        self.assertEqual(
            self.request_urls(requests),
            [
                "https://example.test/ping/abc/start",
                "https://example.test/ping/abc/log",
                "https://example.test/ping/abc/fail",
            ],
        )

    def test_manual_ping_suppresses_automatic_success_ping(self) -> None:
        check, requests = self.make_check()

        with check:
            check.ping_failure()

        self.assertEqual(
            self.request_urls(requests),
            [
                "https://example.test/ping/abc/start",
                "https://example.test/ping/abc/fail",
            ],
        )
