"""Tests for ProseMirror → Markdown converter using fixture pairs."""

import json
from pathlib import Path

import pytest

from docmost_cli.convert.prosemirror_to_md import convert_to_markdown


def test_attachment_paths_override_server_urls() -> None:
    document = {
        "type": "doc",
        "content": [
            {
                "type": "image",
                "attrs": {
                    "attachmentId": "image-id",
                    "src": "/api/files/image-id/old.png",
                    "alt": "Diagram",
                },
            },
            {
                "type": "attachment",
                "attrs": {
                    "attachmentId": "file-id",
                    "url": "/api/files/file-id/old.pdf",
                    "name": "Report.pdf",
                },
            },
        ],
    }

    result = convert_to_markdown(
        document,
        attachment_paths={
            "image-id": "files/image-id/diagram.png",
            "file-id": "files/file-id/Report.pdf",
        },
    )

    assert "![Diagram](files/image-id/diagram.png)" in result
    assert "[Report.pdf](files/file-id/Report.pdf)" in result


FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

# Fixture pairs: (json_file, md_file)
FIXTURE_PAIRS = [
    "simple_paragraph",
    "headings_and_text",
    "lists",
    "code_block",
    "marks",
    "table",
    "empty_doc",
    "supported_rich_content",
    "unsupported_rich_content",
]


@pytest.mark.parametrize("fixture_name", FIXTURE_PAIRS)
def test_fixture_conversion(fixture_name: str) -> None:
    """Convert ProseMirror JSON fixture and compare to expected Markdown."""
    json_path = FIXTURES_DIR / f"{fixture_name}.json"
    md_path = FIXTURES_DIR / f"{fixture_name}.md"

    with open(json_path) as f:
        doc = json.load(f)
    expected = md_path.read_text()

    result = convert_to_markdown(doc)
    assert result == expected, f"Fixture {fixture_name}: output mismatch"


class TestConverterEdgeCases:
    def test_empty_input(self) -> None:
        assert convert_to_markdown({}) == "\n"

    def test_non_dict_input(self) -> None:
        assert convert_to_markdown("not a dict") == ""  # type: ignore[arg-type]

    def test_unknown_node_type(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "unknownCustomNode",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "inner"}]}
                    ],
                }
            ],
        }
        result = convert_to_markdown(doc)
        assert "inner" in result

    def test_horizontal_rule(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Before"}]},
                {"type": "horizontalRule"},
                {"type": "paragraph", "content": [{"type": "text", "text": "After"}]},
            ],
        }
        result = convert_to_markdown(doc)
        assert "---" in result
        assert "Before" in result
        assert "After" in result

    def test_blockquote(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "blockquote",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Quoted text"}]}
                    ],
                }
            ],
        }
        result = convert_to_markdown(doc)
        assert "> Quoted text" in result

    def test_bold_italic_combined(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "both",
                            "marks": [{"type": "bold"}, {"type": "italic"}],
                        }
                    ],
                }
            ],
        }
        result = convert_to_markdown(doc)
        assert "***both***" in result or "**_both_**" in result or "*__both__*" in result

    def test_strikethrough(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "deleted", "marks": [{"type": "strike"}]}],
                }
            ],
        }
        result = convert_to_markdown(doc)
        assert "~~deleted~~" in result

    def test_image(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "image",
                    "attrs": {"src": "https://example.com/img.png", "alt": "A picture"},
                }
            ],
        }
        result = convert_to_markdown(doc)
        assert "![A picture](https://example.com/img.png)" in result

    def test_ordered_list(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "orderedList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "First"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Second"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        result = convert_to_markdown(doc)
        assert "1. First" in result
        assert "2. Second" in result

    def test_task_list(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "taskList",
                    "content": [
                        {
                            "type": "taskItem",
                            "attrs": {"checked": False},
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "Todo"}]}
                            ],
                        },
                        {
                            "type": "taskItem",
                            "attrs": {"checked": True},
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "Done"}]}
                            ],
                        },
                    ],
                }
            ],
        }
        result = convert_to_markdown(doc)
        assert "- [ ] Todo" in result
        assert "- [x] Done" in result
