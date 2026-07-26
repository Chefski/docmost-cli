"""Search subcommand."""

import typer

from docmost_cli.api.pagination import extract_items
from docmost_cli.api.search import search
from docmost_cli.api.spaces import resolve_space_id
from docmost_cli.cli.main import get_client
from docmost_cli.output.formatter import print_table

__all__ = ["search_app"]

search_app = typer.Typer(name="search", help="Search across the wiki.")


@search_app.command("query")
def search_cmd(
    query: str = typer.Argument(help="Search query"),
    space: str | None = typer.Option(None, "--space", help="Filter by space slug"),
    limit: int | None = typer.Option(None, "--limit", help="Max results (server default: 25)"),
    offset: int | None = typer.Option(None, "--offset", help="Number of results to skip"),
    json_mode: bool = typer.Option(False, "--json", help="Output as JSON array"),
) -> None:
    """Full-text search across wiki pages."""
    client = get_client()
    space_id = None
    if space is not None:
        space_id = resolve_space_id(client, space)

    result = search(client, query, space_id=space_id, limit=limit, offset=offset)
    items = extract_items(result)
    columns = ["id", "title", "highlight"]
    print_table(items, columns, json_mode=json_mode)
