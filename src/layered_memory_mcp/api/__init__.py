"""REST API package for Layered Memory v2.0.

Provides HTTP endpoints for knowledge CRUD, search, and review.
Can be used by any agent system, not just Hermes.
"""

from .server import create_app

__all__ = ["create_app"]
