from typing import TypedDict


class HealthCheckState(TypedDict):
    ping_sent: bool
    """
    A flag for remembering whether a ping was sent during the context,
    when using the `HealthCheck` class is used as a context manager.
    """
