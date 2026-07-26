"""User API methods."""

from typing import Any

from docmost_cli.api.client import DocmostClient

__all__ = [
    "get_current_user",
]


def get_current_user(client: DocmostClient) -> dict[str, Any]:
    """Get the currently authenticated user's info.

    Args:
        client: Authenticated Docmost client.

    Returns:
        Unwrapped user info dict.
    """
    result = client.post("/users/me", json={}, retry_safe=True)
    data = result.get("data", result)
    if not isinstance(data, dict):
        return {}
    if "user" in data and isinstance(data["user"], dict):
        return data["user"]
    return data
