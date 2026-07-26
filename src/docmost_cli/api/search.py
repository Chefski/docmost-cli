"""Search API methods."""

from typing import Any

from docmost_cli.api.client import DocmostClient
from docmost_cli.api.pagination import build_body

__all__ = ["search"]


def search(
    client: DocmostClient,
    query: str,
    *,
    space_id: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    """Full-text search across wiki pages.

    Args:
        client: Authenticated Docmost client.
        query: Search query string.
        space_id: Optional space UUID to filter results.
        limit: Maximum number of results (default server-side: 25).
        offset: Number of results to skip.

    Returns:
        Raw API response dict.
    """
    body = build_body(
        {"query": query},
        spaceId=space_id,
        limit=limit,
        offset=offset,
    )
    return client.post("/search", json=body)
