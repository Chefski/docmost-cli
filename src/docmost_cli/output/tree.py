"""Tree view rendering for hierarchical page lists.

Renders nested page structures using Unicode box-drawing characters.
"""

from typing import Any

from rich.console import Console

__all__ = ["print_tree"]

MAX_TITLE_LEN = 60
_console = Console()


def print_tree(pages: list[dict[str, Any]]) -> None:
    """Render a nested page tree using box-drawing characters.

    Expects pages with nested 'children' arrays, as returned by
    POST /pages/sidebar-pages.

    Args:
        pages: List of page dicts, each may have a 'children' key.
    """
    for i, page in enumerate(pages):
        is_last = i == len(pages) - 1
        _print_node(page, "", is_last)


def _print_node(
    page: dict[str, Any],
    prefix: str,
    is_last: bool,
) -> None:
    """Print a node and its descendants without recursive Python calls."""
    stack = [(page, prefix, is_last)]
    while stack:
        current_page, current_prefix, current_is_last = stack.pop()
        connector = "\\-- " if current_is_last else "+-- "

        icon = current_page.get("icon", "") or ""
        title = current_page.get("title", current_page.get("id", "???"))
        if len(title) > MAX_TITLE_LEN:
            title = title[: MAX_TITLE_LEN - 3] + "..."

        safe_icon = ""
        if icon:
            try:
                icon.encode("cp1252")
                safe_icon = icon
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass

        label = f"{safe_icon} {title}".strip() if safe_icon else title
        _console.print(f"{current_prefix}{connector}{label}")

        children = current_page.get("children", [])
        child_prefix = current_prefix + ("    " if current_is_last else "|   ")
        for index in range(len(children) - 1, -1, -1):
            stack.append((children[index], child_prefix, index == len(children) - 1))
