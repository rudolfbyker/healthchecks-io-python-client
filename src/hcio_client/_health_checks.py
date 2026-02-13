from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from logging import getLogger
from typing import Mapping, Iterable, List, Dict, Protocol, Sequence
from urllib.parse import urljoin
from uuid import UUID

from requests import request, Response
from tenacity import retry, stop_after_attempt, wait_fixed

from ._health_check import HealthCheck

logger = getLogger(__name__)


class RequestFunction(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        params: Mapping[str, str | Sequence[str]] | None = None,
        data: str | None = None,
        json: Mapping[str, str | int | float | bool] | None = None,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response: ...


def default_request_function(
    method: str,
    url: str,
    params: Mapping[str, str | Sequence[str]] | None = None,
    data: str | None = None,
    json: Mapping[str, str | int | float | bool] | None = None,
    timeout: float | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    return request(
        method=method,
        url=url,
        params=params,
        data=data,
        json=json,
        timeout=timeout,
        headers=headers,
    )


@dataclass(frozen=True)
class HealthChecks:
    """
    A client for https://healthchecks.io .

    See docs at:
    - https://healthchecks.io/docs/http_api/
    - https://healthchecks.io/docs/api/
    - https://healthchecks.io/docs/autoprovisioning/
    """

    ping_base_url: str = "https://hc-ping.com/"
    manage_base_url: str = "https://healthchecks.io/api/v3/"

    n_ping_attempts: int = 3
    n_wait_between_ping_attempts: float = 2.0
    n_manage_attempts: int = 3
    n_wait_between_manage_attempts: float = 2.0
    request_timeout: float = 3.0
    request_function: RequestFunction = default_request_function

    ping_key: str | None = None
    create: bool | None = None
    run_id: UUID | str | None = None

    manage_key: str | None = None

    log_exceptions: bool = False

    @property
    def _request_function_with_defaults(self) -> RequestFunction:
        return partial(
            self.request_function,
            timeout=self.request_timeout,
        )

    @property
    def request_retry_ping(self) -> RequestFunction:
        return retry(
            stop=stop_after_attempt(self.n_ping_attempts),
            wait=wait_fixed(self.n_wait_between_ping_attempts),
            reraise=True,
        )(self._request_function_with_defaults)

    @property
    def request_retry_manage(self) -> RequestFunction:
        return retry(
            stop=stop_after_attempt(self.n_manage_attempts),
            wait=wait_fixed(self.n_wait_between_manage_attempts),
            reraise=True,
        )(self._request_function_with_defaults)

    def ping(
        self,
        *,
        url: str,
        method: str = "GET",
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        data: str | None = None,
        raise_for_status: bool = False,
        raise_for_failed_request: bool = False,
    ) -> None:
        try:
            response = self.request_retry_ping(
                method=method,
                url=url,
                params=params,
                data=data,
                headers=headers,
            )
        except Exception as e:
            if self.log_exceptions:
                logger.exception(msg=f"Ping failed: {e}")
            if raise_for_failed_request:
                raise e
        else:
            if raise_for_status:
                response.raise_for_status()

    def check(
        self,
        *,
        uuid: UUID | str | None = None,
        ping_key: str | None = None,
        manage_key: str | None = None,
        slug: str | None = None,
        desc: str | None = None,
        create: bool | None = None,
        run_id: UUID | str | None = None,
        timeout: int | None = None,
        grace: int | None = None,
        suppress_on_exit: bool = False,
    ) -> HealthCheck:
        check = HealthCheck(
            hc=self,
            uuid=uuid,
            ping_key=ping_key or self.ping_key,
            manage_key=manage_key or self.manage_key,
            slug=slug,
            create=create if create is not None else self.create,
            run_id=run_id or self.run_id,
            suppress_on_exit=suppress_on_exit,
        )

        if timeout or grace or desc:
            check.manage_update(
                timeout=timeout,
                grace=grace,
                desc=desc,
                raise_for_status=False,
                raise_for_failed_request=False,
            )

        return check

    def list(
        self,
        *,
        slug: str | None = None,
        tags: Iterable[str] | None = None,
    ) -> List[Dict[str, str | int | bool]]:
        if not self.manage_key:
            raise ValueError("`manage_key` must be provided.")

        params: Dict[str, str | List[str]] = {}

        if slug:
            params["slug"] = slug

        if tags:
            params["tag"] = list(tags)

        response = self.request_retry_manage(
            method="GET",
            headers={"X-Api-Key": self.manage_key},
            url=urljoin(self.manage_base_url, "checks"),
            params=params,
        )
        response.raise_for_status()
        result = response.json().get("checks")
        if not isinstance(result, list):
            raise ValueError(f"Expected a list, got `{type(result)}`.")

        return result

    def get_uuid_from_slug(self, *, slug: str) -> str:
        if not self.manage_key:
            raise ValueError("`manage_key` must be provided.")
        all_checks_info = self.list(slug=slug)
        if len(all_checks_info) == 0:
            raise ValueError(f"No check found with slug `{slug}`.")
        if len(all_checks_info) > 1:
            raise ValueError(f"Multiple checks found with slug `{slug}`.")
        check_info = all_checks_info[0]
        if not isinstance(check_info, dict):
            raise ValueError(f"Expected a dict, got `{type(check_info)}`.")
        uuid = check_info.get("uuid")
        if not isinstance(uuid, str):
            raise ValueError(f"Expected a str, got `{type(uuid)}`.")
        return uuid
