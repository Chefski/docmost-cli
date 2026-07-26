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
    result_type: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Full-text search across the wiki.

    Args:
        client: Authenticated Docmost client.
        query: Search query string.
        space_id: Optional space UUID to filter results.
        result_type: Optional type filter ("page" or "attachment").
        limit: Max results (default server-side, typically 20).
        cursor: Pagination cursor.

    Returns:
        Raw API response dict.
    """
    body = build_body(
        {"query": query},
        spaceId=space_id,
        type=result_type,
        limit=limit,
        cursor=cursor,
    )
    return client.post("/search", json=body, retry_safe=True)
