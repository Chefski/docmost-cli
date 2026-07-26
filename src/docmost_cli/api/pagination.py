"""Pagination utilities for cursor-based API responses.

Provides shared helpers for extracting items from varying response shapes
and auto-following pagination cursors.
"""

from collections.abc import Callable
from typing import Any, cast

__all__ = ["build_body", "extract_id", "extract_items", "get_cursor", "paginate_all"]


def extract_id(response: dict[str, Any]) -> str:
    """Extract resource ID from API response, handling nested shapes."""
    resource_id = response.get("id") or response.get("data", {}).get("id", "")
    return str(resource_id) if resource_id else ""


def build_body(required: dict[str, Any], **optional: Any) -> dict[str, Any]:
    """Build API request body, filtering out None optional values."""
    body = dict(required)
    for key, value in optional.items():
        if value is not None:
            body[key] = value
    return body


def extract_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract items list from API response, handling nested shapes.

    Handles: {data: {items: [...]}}, {data: [...]}, {items: [...]}, and flat dicts.

    Args:
        response: Raw API response dict.

    Returns:
        List of item dicts.
    """
    if "data" in response and isinstance(response["data"], dict):
        items = response["data"].get("items", [])
        return cast("list[dict[str, Any]]", items) if isinstance(items, list) else []
    if "data" in response and isinstance(response["data"], list):
        return cast("list[dict[str, Any]]", response["data"])
    items = response.get("items", [response] if "id" in response else [])
    return cast("list[dict[str, Any]]", items) if isinstance(items, list) else []


def get_cursor(response: dict[str, Any]) -> str | None:
    """Extract pagination cursor from API response.

    Args:
        response: Raw API response dict.

    Returns:
        Next cursor string, or None if no more pages.
    """
    if "data" in response and isinstance(response["data"], dict):
        return response["data"].get("cursor")
    return response.get("cursor")


def paginate_all(
    fetch_func: Callable[..., dict[str, Any]],
    *,
    limit: int | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Auto-follow pagination until exhausted or limit reached.

    Calls fetch_func repeatedly with cursor parameter until no more
    pages are available or the total item count reaches limit.

    Args:
        fetch_func: API function that accepts cursor= keyword arg.
        limit: Max total items to collect (None = all).
        **kwargs: Additional arguments passed to fetch_func.

    Returns:
        Combined list of all items across pages.
    """
    max_iterations = 1000  # Safety guard against infinite loops
    all_items: list[dict[str, Any]] = []
    cursor: str | None = None

    for _ in range(max_iterations):
        response = fetch_func(**kwargs, cursor=cursor)
        items = extract_items(response)
        all_items.extend(items)

        if limit and len(all_items) >= limit:
            return all_items[:limit]

        cursor = get_cursor(response)
        if not cursor:
            break

    return all_items
