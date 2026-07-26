"""Space API methods."""

from typing import Any

from docmost_cli.api.client import DocmostClient
from docmost_cli.api.pagination import build_body
from docmost_cli.output.formatter import print_error

__all__ = [
    "create_space",
    "get_space_info",
    "list_spaces",
    "resolve_space_id",
    "update_space",
]


def list_spaces(
    client: DocmostClient,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List spaces with optional pagination.

    Args:
        client: Authenticated Docmost client.
        limit: Max results to return.
        cursor: Pagination cursor.

    Returns:
        Raw API response dict.
    """
    body = build_body({}, limit=limit, cursor=cursor)
    return client.post("/spaces", json=body, retry_safe=True)


def get_space_info(
    client: DocmostClient,
    *,
    slug: str | None = None,
    space_id: str | None = None,
) -> dict[str, Any]:
    """Get space info by slug or ID.

    Args:
        client: Authenticated Docmost client.
        slug: Space slug (e.g., "engineering").
        space_id: Space UUID.

    Returns:
        Space info dict.
    """
    if space_id:
        result = client.post("/spaces/info", json={"spaceId": space_id}, retry_safe=True)
        data = result.get("data", result)
        return data if isinstance(data, dict) else {}
    if slug:
        # /spaces/info only accepts spaceId, so search by slug in the full list
        return _find_space_by_slug(client, slug)
    print_error("Either slug or space_id is required.", exit_code=1)


def _find_space_by_slug(client: DocmostClient, slug: str) -> dict[str, Any]:
    """Find a space by slug from the spaces list.

    Args:
        client: Authenticated Docmost client.
        slug: Space slug to find.

    Returns:
        Space info dict.
    """
    from docmost_cli.api.pagination import extract_items

    result = list_spaces(client)
    items = extract_items(result)
    for space in items:
        if space.get("slug") == slug:
            return space
    print_error(f"Space '{slug}' not found.", exit_code=4)


def resolve_space_id(client: DocmostClient, slug: str) -> str:
    """Resolve a space slug to its UUID.

    Args:
        client: Authenticated Docmost client.
        slug: Space slug.

    Returns:
        Space UUID string.
    """
    info = get_space_info(client, slug=slug)
    space_id = info.get("id")
    if not space_id:
        print_error(f"Space '{slug}' not found.", exit_code=4)
    return str(space_id)


def create_space(
    client: DocmostClient,
    *,
    name: str,
    slug: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a new space.

    Args:
        client: Authenticated Docmost client.
        name: Space name.
        slug: Space slug (auto-generated if omitted).
        description: Space description.

    Returns:
        Raw API response dict.
    """
    body = build_body({"name": name}, slug=slug, description=description)
    return client.post("/spaces/create", json=body)


def update_space(
    client: DocmostClient,
    *,
    space_id: str,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Update an existing space.

    Args:
        client: Authenticated Docmost client.
        space_id: Space UUID.
        name: New space name.
        description: New description.

    Returns:
        Raw API response dict.
    """
    body = build_body({"spaceId": space_id}, name=name, description=description)
    return client.post("/spaces/update", json=body)
