"""Comment API methods."""

import json
from typing import Any

from docmost_cli.api.client import DocmostClient

__all__ = [
    "create_comment",
    "list_comments",
    "update_comment",
]


def _wrap_text_as_prosemirror(text: str) -> dict[str, Any]:
    """Wrap plain text into a minimal ProseMirror document.

    Creates the structure Docmost expects for comment content.
    Multi-line text is split into separate paragraphs.

    Args:
        text: Plain text content.

    Returns:
        ProseMirror document dict.
    """
    paragraphs = []
    for line in text.split("\n"):
        if line.strip():
            paragraphs.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": line}],
                }
            )
        else:
            paragraphs.append({"type": "paragraph"})

    if not paragraphs:
        paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]

    return {"type": "doc", "content": paragraphs}


def list_comments(client: DocmostClient, page_id: str) -> dict[str, Any]:
    """List all comments on a page.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID.

    Returns:
        Raw API response dict.
    """
    return client.post("/comments", json={"pageId": page_id}, retry_safe=True)


def create_comment(
    client: DocmostClient,
    *,
    page_id: str,
    content: str,
) -> dict[str, Any]:
    """Create a comment on a page.

    Content is wrapped in ProseMirror JSON format as required by the API.

    Args:
        client: Authenticated Docmost client.
        page_id: Page UUID.
        content: Plain text comment content.

    Returns:
        Raw API response dict.
    """
    pm_content = _wrap_text_as_prosemirror(content)
    return client.post(
        "/comments/create",
        json={"pageId": page_id, "content": json.dumps(pm_content)},
    )


def update_comment(
    client: DocmostClient,
    *,
    comment_id: str,
    content: str,
) -> dict[str, Any]:
    """Update an existing comment.

    Args:
        client: Authenticated Docmost client.
        comment_id: Comment UUID.
        content: New plain text content.

    Returns:
        Raw API response dict.
    """
    pm_content = _wrap_text_as_prosemirror(content)
    return client.post(
        "/comments/update",
        json={"commentId": comment_id, "content": json.dumps(pm_content)},
    )
