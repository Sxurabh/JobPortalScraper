"""Central host-to-handler registry for career portal adapters.

PortalScraper retains the public entry point and owns the shared HTTP session.
Provider mixins live in separate modules; this registry maps their entry points
to career-site hosts without coupling the adapters to one another.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PortalRoute:
    hosts: tuple[str, ...]
    handler: str


ROUTES = (
    PortalRoute(("careers.statestreet.com",), "_statestreet"),
    PortalRoute(("careers.fisglobal.com",), "_fisglobal"),
    PortalRoute(("careers.mastercard.com",), "_mastercard"),
    PortalRoute(("careers.blackrock.com",), "_blackrock"),
    PortalRoute(("jobs.ubs.com",), "_ubs"),
    PortalRoute(("careers.bp.com",), "_bp"),
    PortalRoute(("jobs.citi.com",), "_citi"),
    PortalRoute(("addepar.com",), "_addepar"),
    PortalRoute(("greenhouse.io",), "_greenhouse"),
    PortalRoute(("myworkdayjobs.com", "myworkdaysite.com"), "_workday"),
)


def handler_for(hostname: str) -> str | None:
    host = (hostname or "").casefold().rstrip(".")
    for route in ROUTES:
        if any(host == domain or host.endswith("." + domain) for domain in route.hosts):
            return route.handler
    return None
