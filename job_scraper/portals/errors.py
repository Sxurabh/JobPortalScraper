"""Errors shared by portal adapters."""


class PortalStructureError(RuntimeError):
    """A supported portal no longer exposes the listing structure we expect."""
