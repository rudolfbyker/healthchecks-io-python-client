from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from functools import cached_property
from logging import getLogger
from textwrap import indent
from types import TracebackType
from typing import TYPE_CHECKING, Self, Dict
from urllib.parse import urljoin
from uuid import UUID

from ._health_check_state import HealthCheckState
from ._json_types import JsonValue

if TYPE_CHECKING:
    from ._health_checks import HealthChecks

logger = getLogger(__name__)


@dataclass(frozen=True)
class HealthCheck:
    """
    A client for a single health check on https://healthchecks.io .

    See docs at:
    - https://healthchecks.io/docs/http_api/
    - https://healthchecks.io/docs/api/
    - https://healthchecks.io/docs/autoprovisioning/

    This can be used as a context manager to automatically send pings on start and exit.
    If `ping_success` or `ping_failure` is called during the context, no success ping will be sent on exit.
    Otherwise, a success ping will be sent if no exception was raised.
    A failure ping will always be sent if an exception was raised.
    """

    hc: "HealthChecks"
    uuid: UUID | str | None = None
    ping_key: str | None = None
    manage_key: str | None = None
    slug: str | None = None
    create: bool | None = None
    run_id: UUID | str | None = None

    suppress_on_exit: bool = False
    """
    Whether to suppress the exceptions from the context when exiting the context.
    
    This is only applicable when using this class as a context manager.
    This does not affect whether the exceptions are logged or not.
    This does not affect whether the failure ping is sent or not.
    """

    _state: HealthCheckState = field(
        default_factory=lambda: HealthCheckState(ping_sent=False),
        init=False,
        repr=False,
    )

    @property
    def description(self) -> str:
        if self.slug and self.uuid:
            return f"health check with slug={self.slug} and uuid={self.uuid}"
        if self.slug:
            return f"health check with slug={self.slug}"
        if self.uuid:
            return f"health check with uuid={self.uuid}"
        return "unknown health check"

    @cached_property
    def ping_base_url(self) -> str:
        if self.uuid:
            return urljoin(self.hc.ping_base_url, str(self.uuid))

        if self.ping_key and self.slug:
            return urljoin(self.hc.ping_base_url, f"{self.ping_key}/{self.slug}")

        raise ValueError("Either `uuid` or (`ping_key` and `slug`) must be provided.")

    @cached_property
    def manage_base_url(self) -> str:
        if self.uuid:
            uuid = self.uuid
        elif self.slug and self.manage_key:
            uuid = self.hc.get_uuid_from_slug(
                slug=self.slug,
                manage_key=self.manage_key,
            )
        else:
            raise ValueError(
                "Either `uuid` or (`slug` and `manage_key`) must be provided."
            )

        return urljoin(self.hc.manage_base_url, f"checks/{uuid}")

    def ping_success(
        self,
        *,
        raise_for_status: bool = False,
        raise_for_failed_request: bool = False,
    ) -> None:
        """
        Send a success ping for this health check.

        If this class is used as a context manager,
        and this method is called within the context,
        no automatic success ping will be sent when exiting the context.

        Args:
            raise_for_status: Whether to raise an exception if the ping request fails.
            raise_for_failed_request: Whether to raise an exception if the ping request returns status code >= 400.
        """
        logger.debug("Sending success ping for %s.", self.description)
        self.hc.ping(
            method="GET",
            url=self.ping_base_url,
            params=process_ping_query_args(create=self.create, run_id=self.run_id),
            raise_for_status=raise_for_status,
            raise_for_failed_request=raise_for_failed_request,
        )
        self._state["ping_sent"] = True

    def ping_start(
        self,
        *,
        raise_for_status: bool = False,
        raise_for_failed_request: bool = False,
    ) -> None:
        """
        Send a start ping for this health check.

        If this class is used as a context manager,
        this is automatically called when entering the context.

        Args:
            raise_for_status: Whether to raise an exception if the ping request fails.
            raise_for_failed_request: Whether to raise an exception if the ping request returns status code >= 400.
        """
        logger.debug("Sending start ping for %s.", self.description)
        self.hc.ping(
            method="GET",
            url=f"{self.ping_base_url}/start",
            params=process_ping_query_args(create=self.create, run_id=self.run_id),
            raise_for_status=raise_for_status,
            raise_for_failed_request=raise_for_failed_request,
        )

    def ping_failure(
        self,
        *,
        raise_for_status: bool = False,
        raise_for_failed_request: bool = False,
    ) -> None:
        """
        Send a failure ping for this health check.

        If this class is used as a context manager,
        and an exception is raised within the context,
        this is called automatically upon exiting the context.

        If this class is used as a context manager,
        and this method is called within the context,
        no automatic success ping will be sent when exiting the context.

        Args:
            raise_for_status: Whether to raise an exception if the ping request fails.
            raise_for_failed_request: Whether to raise an exception if the ping request returns status code >= 400.
        """
        logger.debug("Sending failure ping for %s.", self.description)
        self.hc.ping(
            method="GET",
            url=f"{self.ping_base_url}/fail",
            params=process_ping_query_args(create=self.create, run_id=self.run_id),
            raise_for_status=raise_for_status,
            raise_for_failed_request=raise_for_failed_request,
        )
        self._state["ping_sent"] = True

    def ping_exit_code(
        self,
        *,
        code: int,
        raise_for_status: bool = False,
        raise_for_failed_request: bool = False,
    ) -> None:
        """
        Send an exit code ping for this health check.

        If this class is used as a context manager,
        and this method is called within the context,
        no automatic success ping will be sent when exiting the context.

        Args:
            code: The exit code to send. By default, `0` means `success` and everything else means `failure`.
            raise_for_status: Whether to raise an exception if the ping request fails.
            raise_for_failed_request: Whether to raise an exception if the ping request returns status code >= 400.
        """
        logger.debug("Sending exit code {code} for %s.", self.description)
        self.hc.ping(
            method="GET",
            url=f"{self.ping_base_url}/{code}",
            params=process_ping_query_args(create=self.create, run_id=self.run_id),
            raise_for_status=raise_for_status,
            raise_for_failed_request=raise_for_failed_request,
        )
        self._state["ping_sent"] = True

    def ping_log_exception(
        self,
        *,
        e: BaseException,
        raise_for_status: bool = False,
        raise_for_failed_request: bool = False,
    ) -> None:
        """
        Send a log ping for this health check containing exception details.

        If this class is used as a context manager,
        and an exception is raised within the context,
        this is called automatically upon exiting the context.

        Args:
            e: The exception to log.
            raise_for_status: Whether to raise an exception if the ping request fails.
            raise_for_failed_request: Whether to raise an exception if the ping request returns status code >= 400.
        """
        self.ping_log(
            data=format_exception_for_health_checks_log(e),
            raise_for_status=raise_for_status,
            raise_for_failed_request=raise_for_failed_request,
        )

    def ping_log(
        self,
        *,
        data: str,
        raise_for_status: bool = False,
        raise_for_failed_request: bool = False,
    ) -> None:
        """
        Send a log ping for this health check containing arbitrary data.

        Args:
            data: The data to send in the ping.
            raise_for_status: Whether to raise an exception if the ping request fails.
            raise_for_failed_request: Whether to raise an exception if the ping request returns status code >= 400.
        """
        logger.debug("Sending logs for %s:\n%s", self.description, data)
        self.hc.ping(
            method="POST",
            url=f"{self.ping_base_url}/log",
            params=process_ping_query_args(create=self.create, run_id=self.run_id),
            data=data,
            raise_for_status=raise_for_status,
            raise_for_failed_request=raise_for_failed_request,
        )

    def __enter__(self) -> Self:
        logger.debug("Entering context for %s", self.description)
        self._state["ping_sent"] = False
        self.ping_start(
            raise_for_status=False,
            raise_for_failed_request=False,
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_val: BaseException | None = None,
        exc_tb: TracebackType | None = None,
    ) -> bool:
        if exc_type is None:
            logger.debug("Context for %s exited without exception.", self.description)
            if not self._state["ping_sent"]:
                self.ping_success(
                    raise_for_status=False,
                    raise_for_failed_request=False,
                )
        else:
            logger.debug(
                "Context for %s exited with exception `%s`:\n%s",
                self.description,
                exc_type.__name__,
                str(exc_val),
            )
            if exc_val is not None:
                self.ping_log_exception(
                    e=exc_val,
                    raise_for_status=False,
                    raise_for_failed_request=False,
                )
            self.ping_failure(
                raise_for_status=False,
                raise_for_failed_request=False,
            )

        return self.suppress_on_exit

    def manage_get(self) -> Dict[str, str | int | bool]:
        if not self.manage_key:
            raise ValueError("`manage_key` must be provided.")

        response = self.hc.request_retry_manage(
            method="GET",
            url=self.manage_base_url,
            headers={"X-Api-Key": self.manage_key},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError(f"Expected a dict, got `{type(result)}`.")
        return result

    def manage_delete(
        self,
        *,
        raise_for_status: bool = True,
        raise_for_failed_request: bool = True,
    ) -> None:
        if not self.manage_key:
            raise ValueError("`manage_key` must be provided.")

        try:
            response = self.hc.request_retry_manage(
                method="DELETE",
                url=self.manage_base_url,
                headers={"X-Api-Key": self.manage_key},
            )
        except Exception as e:
            if raise_for_failed_request:
                raise e
        else:
            if raise_for_status:
                response.raise_for_status()

    def manage_update(
        self,
        *,
        name: str | None = None,
        slug: str | None = None,
        tags: str | None = None,
        desc: str | None = None,
        timeout: int | None = None,
        grace: int | None = None,
        schedule: str | None = None,
        tz: str | None = None,
        raise_for_status: bool = False,
        raise_for_failed_request: bool = False,
    ) -> None:
        if not self.manage_key:
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

        try:
            response = self.hc.request_retry_manage(
                method="POST",
                url=self.manage_base_url,
                headers={"X-Api-Key": self.manage_key},
                json=data,
            )
        except Exception as e:
            if raise_for_failed_request:
                raise e
        else:
            if raise_for_status:
                response.raise_for_status()


def process_ping_query_args(
    *,
    create: bool | None = None,
    run_id: UUID | str | None = None,
) -> Dict[str, str]:
    params: Dict[str, str] = {}

    if create is not None:
        params["create"] = str(int(create))

    if run_id is not None:
        params["rid"] = str(run_id)

    return params


def process_manage_check_data(
    *,
    name: str | None = None,
    slug: str | None = None,
    tags: str | None = None,
    desc: str | None = None,
    timeout: int | None = None,
    grace: int | None = None,
    schedule: str | None = None,
    tz: str | None = None,
) -> Dict[str, JsonValue]:
    data: Dict[str, JsonValue] = {}
    if name is not None:
        data["name"] = name
    if slug is not None:
        data["slug"] = slug
    if tags is not None:
        data["tags"] = tags
    if desc is not None:
        data["desc"] = desc
    if timeout is not None:
        data["timeout"] = timeout
    if grace is not None:
        data["grace"] = grace
    if schedule is not None:
        data["schedule"] = schedule
    if tz is not None:
        data["tz"] = tz
    return data


def format_exception_for_health_checks_log(e: BaseException) -> str:
    """
    Examples:
        >>> try:  # doctest: +ELLIPSIS
        ...     raise RuntimeError('''meh
        ... not good''')
        ... except RuntimeError as e1:
        ...     print(format_exception_for_health_checks_log(e1))
        Exception type: RuntimeError
        <BLANKLINE>
        Exception details:
          meh
          not good
        <BLANKLINE>
        Traceback:
          ...
    """
    tb = "".join(traceback.format_tb(e.__traceback__)).rstrip("\n")

    details = indent(text=str(e), prefix="  ")

    return f"""\
Exception type: {type(e).__name__}

Exception details:
{details}

Traceback:
{tb}"""
