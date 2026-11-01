"""Errors shared by portal adapters."""


class PortalStructureError(RuntimeError):
    """A supported portal no longer exposes the listing structure we expect."""


def classify_scan_error(error: Exception) -> str:
    """Classify failures for health reporting and safe retry scheduling."""
    if isinstance(error, PortalStructureError):
        return "structure_changed"

    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429 or (isinstance(status, int) and status >= 500):
        return "transient_network"
    if isinstance(status, int) and 400 <= status < 500:
        return "remote_rejected"

    name = type(error).__name__.casefold()
    message = str(error).casefold()
    if "targetclosed" in name or ("page" in message and "browser has been closed" in message):
        return "browser_runtime"
    if any(word in name for word in ("timeout", "connectionerror", "connectionreset", "connecterror")):
        return "transient_network"
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return "transient_network"
    return "unknown"


def scan_retry_delay_seconds(category: str, failure_streak: int) -> int:
    """Return a bounded retry delay; healthy companies are never delayed."""
    if category in {"transient_network", "browser_runtime"}:
        return min(600 * (2 ** max(0, failure_streak - 1)), 6 * 60 * 60)
    if category in {"structure_changed", "remote_rejected"}:
        return 6 * 60 * 60
    return 60 * 60
