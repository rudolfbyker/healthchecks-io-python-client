from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from logging import getLogger
from typing import Callable, Mapping, Iterable, List, Dict, Protocol, Sequence, TypeVar
from urllib.parse import urljoin
from uuid import UUID

from requests import ConnectionError, HTTPError, Timeout, request, Response
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from ._health_check import HealthCheck, process_manage_check_data
from ._json_types import JsonObject, JsonValue

logger = getLogger(__name__)
T = TypeVar("T")


def _is_transient_configuration_error(error: BaseException) -> bool:
    """
    Identify transport errors and HTTP 5xx responses eligible for setup retries.

    Args:
        error: Exception raised during implicit configuration.

    Returns:
        Whether configuration may be retried and eventually skipped.
    """
    return isinstance(error, (ConnectionError, Timeout)) or (
        isinstance(error, HTTPError)
        and error.response is not None
        and 500 <= error.response.status_code < 600
    )


class RequestFunction(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        params: Mapping[str, str | Sequence[str]] | None = None,
        data: str | None = None,
        json: JsonObject | None = None,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response: ...


def default_request_function(
    method: str,
    url: str,
    params: Mapping[str, str | Sequence[str]] | None = None,
    data: str | None = None,
    json: JsonObject | None = None,
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
        name: str | None = None,
        slug: str | None = None,
        desc: str | None = None,
        create: bool | None = None,
        run_id: UUID | str | None = None,
        timeout: int | None = None,
        grace: int | None = None,
        suppress_exceptions_on_exit: bool = False,
        suppress_success_ping_on_exit: bool = False,
        suppress_failure_ping_on_exit: bool = False,
    ) -> HealthCheck:
        """
        Build a check and configure supplied metadata before its first ping.

        Transient transport errors and HTTP 5xx responses are retried using the
        management retry budget. Exhausted transient failures leave the check
        available for normal task pings. Other configuration errors propagate.

        Args:
            uuid: Existing check UUID, if known.
            ping_key: Per-check ping key overriding the client default.
            manage_key: Per-check management key overriding the client default.
            name: Display name to configure.
            slug: Check slug, required with a ping key when no UUID is supplied.
            desc: Description to configure.
            create: Whether slug pings may create checks; overrides the client default.
            run_id: Run identifier overriding the client default.
            timeout: Expected interval between pings, in seconds.
            grace: Grace period, in seconds.
            suppress_exceptions_on_exit: Whether context exceptions are suppressed.
            suppress_success_ping_on_exit: Whether automatic success pings are skipped.
            suppress_failure_ping_on_exit: Whether automatic failure pings are skipped.

        Returns:
            A check retaining its identity and normal task outcome reporting.
        """
        resolved_create = create if create is not None else self.create
        resolved_manage_key = manage_key or self.manage_key
        check_config_fields = {
            "name": name,
            "desc": desc,
            "timeout": timeout,
            "grace": grace,
        }
        should_configure_check = any(
            value is not None for value in check_config_fields.values()
        )
        # The outer retry owns the full budget, including HTTP status failures.
        setup_hc = replace(self, n_manage_attempts=1)

        if should_configure_check and uuid is None and slug and resolved_create:
            if not resolved_manage_key:
                field_names = ", ".join(
                    field_name
                    for field_name, value in check_config_fields.items()
                    if value is not None
                )
                raise ValueError(
                    f"`manage_key` must be provided to set `{field_names}` "
                    "before creating or updating a slug-based check."
                )

            configure = partial(
                setup_hc.create_or_update,
                manage_key=resolved_manage_key,
                name=name,
                slug=slug,
                desc=desc,
                timeout=timeout,
                grace=grace,
                unique=("slug",),
            )
            try:
                check_info = self._retry_configuration(func=configure)
            except Exception as error:
                if not _is_transient_configuration_error(error):
                    raise
                if self.log_exceptions:
                    logger.exception(
                        "Check configuration failed; continuing with slug pings."
                    )
            else:
                configured_uuid = check_info.get("uuid")
                if not isinstance(configured_uuid, str) or not configured_uuid:
                    raise ValueError(
                        "Expected Healthchecks.io to return a non-empty `uuid` str."
                    )
                uuid = configured_uuid
                resolved_create = None

            # Do not repeat an exhausted upsert as a separate metadata update.
            should_configure_check = False

        check = HealthCheck(
            hc=self,
            uuid=uuid,
            ping_key=ping_key or self.ping_key,
            manage_key=resolved_manage_key,
            slug=slug,
            create=resolved_create,
            run_id=run_id or self.run_id,
            suppress_exceptions_on_exit=suppress_exceptions_on_exit,
            suppress_success_ping_on_exit=suppress_success_ping_on_exit,
            suppress_failure_ping_on_exit=suppress_failure_ping_on_exit,
        )

        if should_configure_check:
            configure_update = partial(
                replace(check, hc=setup_hc).manage_update,
                name=name,
                timeout=timeout,
                grace=grace,
                desc=desc,
                raise_for_status=True,
                raise_for_failed_request=True,
            )
            try:
                self._retry_configuration(func=configure_update)
            except Exception as error:
                if not _is_transient_configuration_error(error):
                    raise
                if self.log_exceptions:
                    logger.exception(
                        "Check configuration failed; continuing with available pings."
                    )

        return check

    def _retry_configuration(self, *, func: Callable[[], T]) -> T:
        """
        Retry implicit configuration within the management request budget.

        Args:
            func: Configuration operation using a client with one request attempt.

        Returns:
            The configuration operation's result.
        """
        return retry(
            retry=retry_if_exception(_is_transient_configuration_error),
            stop=stop_after_attempt(self.n_manage_attempts),
            wait=wait_fixed(self.n_wait_between_manage_attempts),
            reraise=True,
        )(func)()

    def create_or_update(
        self,
        *,
        manage_key: str | None = None,
        name: str | None = None,
        slug: str | None = None,
        tags: str | None = None,
        desc: str | None = None,
        timeout: int | None = None,
        grace: int | None = None,
        schedule: str | None = None,
        tz: str | None = None,
        unique: Sequence[str] | None = None,
    ) -> Dict[str, JsonValue]:
        resolved_manage_key = manage_key or self.manage_key
        if not resolved_manage_key:
            raise ValueError("`manage_key` must be provided.")

        data = process_manage_check_data(
            name=name,
            slug=slug,
            tags=tags,
            desc=desc,
            timeout=timeout,
            grace=grace,
            schedule=schedule,
            tz=tz,
        )
        if unique is not None:
            data["unique"] = [value for value in unique]

        response = self.request_retry_manage(
            method="POST",
            headers={"X-Api-Key": resolved_manage_key},
            url=urljoin(self.manage_base_url, "checks/"),
            json=data,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError(f"Expected a dict, got `{type(result)}`.")
        return result

    def list(
        self,
        *,
        slug: str | None = None,
        tags: Iterable[str] | None = None,
        manage_key: str | None = None,
    ) -> List[Dict[str, str | int | bool]]:
        resolved_manage_key = manage_key or self.manage_key
        if not resolved_manage_key:
            raise ValueError("`manage_key` must be provided.")

        params: Dict[str, str | List[str]] = {}

        if slug:
            params["slug"] = slug

        if tags:
            params["tag"] = list(tags)

        response = self.request_retry_manage(
            method="GET",
            headers={"X-Api-Key": resolved_manage_key},
            url=urljoin(self.manage_base_url, "checks"),
            params=params,
        )
        response.raise_for_status()
        result = response.json().get("checks")
        if not isinstance(result, list):
            raise ValueError(f"Expected a list, got `{type(result)}`.")

        return result

    def get_uuid_from_slug(
        self,
        *,
        slug: str,
        manage_key: str | None = None,
    ) -> str:
        all_checks_info = self.list(slug=slug, manage_key=manage_key)
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
