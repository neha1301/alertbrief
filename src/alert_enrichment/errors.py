"""Domain errors, kept separate so the API layer can map them to HTTP codes."""

from __future__ import annotations


class AlertEnrichmentError(Exception):
    """Base class for expected, handled failures."""


class ResourceNotFound(AlertEnrichmentError):
    """The Kubernetes object referenced by an alert does not exist (anymore)."""


class ClusterUnavailable(AlertEnrichmentError):
    """The Kubernetes API could not be reached or rejected the request."""


class PublishError(AlertEnrichmentError):
    """The Slack message could not be delivered."""
