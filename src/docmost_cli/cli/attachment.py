"""Attachment subcommands."""

import json
import sys
from html import escape
from pathlib import Path

import typer

from docmost_cli.api.attachments import (
    download_attachment,
    get_attachment_info,
    search_attachments,
    upload_attachment,
)
from docmost_cli.api.pages import update_page_content
from docmost_cli.api.pagination import extract_items
from docmost_cli.api.spaces import resolve_space_id
from docmost_cli.cli.main import get_client, state
from docmost_cli.output.formatter import _err_console as _err
from docmost_cli.output.formatter import print_error, print_result, print_table

__all__ = ["attachment_app"]

attachment_app: typer.Typer = typer.Typer(name="attachment", help="Attachment operations.")


def _attachment_html(attachment: dict[str, object]) -> str:
    """Build Docmost HTML that retains an attachment's stable ID."""
    attachment_id = escape(str(attachment["id"]), quote=True)
    file_name = escape(str(attachment["fileName"]), quote=True)
    mime_type = escape(str(attachment.get("mimeType") or "application/octet-stream"), quote=True)
    file_size = escape(str(attachment.get("fileSize") or 0), quote=True)
    path = escape(str(attachment["path"]), quote=True)

    if mime_type.startswith("image/"):
        return (
            f'<img src="{path}" alt="{file_name}" '
            f'data-attachment-id="{attachment_id}" data-size="{file_size}">'
        )

    return (
        f'<div data-type="attachment" data-attachment-url="{path}" '
        f'data-attachment-name="{file_name}" data-attachment-mime="{mime_type}" '
        f'data-attachment-size="{file_size}" data-attachment-id="{attachment_id}"></div>'
    )


def _print_attachment(attachment: dict[str, object], *, json_mode: bool, message: str) -> None:
    """Print an attachment result while keeping stdout automation-friendly."""
    if json_mode:
        sys.stdout.write(json.dumps(attachment, indent=2, default=str) + "\n")
        _err.print(message)
        return
    print_result(str(attachment["id"]), f"{message}: {attachment.get('url', '')}")


@attachment_app.command("upload")
def attachment_upload_cmd(
    page_id: str = typer.Argument(help="Page ID that will own the attachment"),
    file: Path = typer.Option(..., "--file", help="File or image to upload"),
    replace: str | None = typer.Option(
        None,
        "--replace",
        help="Replace an existing attachment ID in place (same extension required)",
    ),
    no_insert: bool = typer.Option(
        False,
        "--no-insert",
        help="Upload without inserting a new image/file block into the page",
    ),
    json_mode: bool = typer.Option(False, "--json", help="Return metadata, ID, and URL as JSON"),
) -> None:
    """Upload a file to a page and insert it into the page content.

    A replacement preserves the existing attachment ID/URL and does not insert
    another block. New uploads are appended to the page unless --no-insert is set.
    """
    if not file.is_file():
        print_error(f"File not found: {file}")

    client = get_client()
    attachment = upload_attachment(
        client,
        page_id=page_id,
        file_path=file,
        attachment_id=replace,
    )

    if not replace and not no_insert:
        update_page_content(
            client,
            page_id=page_id,
            content=_attachment_html(attachment),
            fmt="html",
            operation="append",
        )

    action = "Replaced attachment" if replace else "Uploaded attachment"
    _print_attachment(attachment, json_mode=json_mode, message=f"{action} '{file.name}'")


@attachment_app.command("info")
def attachment_info_cmd(
    attachment_id: str = typer.Argument(help="Attachment ID"),
    json_mode: bool = typer.Option(False, "--json", help="Return metadata and URL as JSON"),
) -> None:
    """Get attachment metadata and its stable authenticated URL."""
    attachment = get_attachment_info(get_client(), attachment_id)
    if json_mode:
        sys.stdout.write(json.dumps(attachment, indent=2, default=str) + "\n")
        return
    print_table([attachment], ["id", "fileName", "mimeType", "fileSize", "url"])


@attachment_app.command("download")
def attachment_download_cmd(
    attachment_id: str = typer.Argument(help="Attachment ID"),
    output: Path | None = typer.Option(None, "--output", help="Destination file or directory"),
) -> None:
    """Download an attachment by stable ID."""
    info, content = download_attachment(get_client(), attachment_id)
    destination = output or Path(str(info["fileName"]))
    if destination.is_dir():
        destination = destination / str(info["fileName"])
    if destination.exists() and not state.yes:
        typer.confirm(f"File '{destination}' already exists. Overwrite?", abort=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    print_result(str(info["id"]), f"Downloaded attachment to {destination}")


@attachment_app.command("search")
def attachment_search_cmd(
    query: str = typer.Argument(..., help="Search query string"),
    space: str | None = typer.Option(None, "--space", help="Space slug to scope search"),
    json_mode: bool = typer.Option(False, "--json", help="Output as JSON array"),
) -> None:
    """Search attachments."""
    client = get_client()
    space_id = None
    if space:
        space_id = resolve_space_id(client, space)
    result = search_attachments(client, query, space_id=space_id)
    items = extract_items(result)
    columns = ["id", "fileName", "type"]
    print_table(items, columns, json_mode=json_mode)
