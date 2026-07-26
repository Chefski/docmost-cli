#!/usr/bin/env python3
"""Check the CLI's pinned API contract against a Docmost source checkout."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "tests" / "contracts" / "docmost-v0.95.0.json"


@dataclass(frozen=True)
class Route:
    method: str
    path: str


@dataclass(frozen=True)
class HandlerBinding:
    name: str
    body_types: frozenset[str]


def _normalize_path(*parts: str) -> str:
    joined = "/".join(part.strip("/") for part in parts if part.strip("/"))
    return f"/{joined}" if joined else "/"


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError as exc:
        raise AssertionError(f"missing upstream contract source: {path}") from exc


def _quoted_argument(raw: str) -> str:
    match = re.fullmatch(r"\s*(['\"])(.*?)\1\s*", raw, re.DOTALL)
    if not match:
        raise AssertionError(f"unsupported route decorator argument: {raw!r}")
    return match.group(2)


def _balanced_delimited(source: str, start: int, opening: str, closing: str) -> str:
    opening_index = source.find(opening, start)
    if opening_index < 0:
        raise AssertionError(f"declaration has no opening {opening}")
    depth = 0
    for index in range(opening_index, len(source)):
        character = source[index]
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return source[opening_index + 1 : index]
    raise AssertionError(f"declaration has no closing {closing}")


def controller_bindings(source: str) -> dict[Route, HandlerBinding]:
    """Extract simple NestJS GET/POST routes and their handler body types."""
    controller_match = re.search(r"@Controller\((.*?)\)", source, re.DOTALL)
    if not controller_match:
        raise AssertionError("controller source has no @Controller decorator")
    raw_prefix = controller_match.group(1).strip()
    prefix = _quoted_argument(raw_prefix) if raw_prefix else ""

    bindings: dict[Route, HandlerBinding] = {}
    for match in re.finditer(r"@(Get|Post)\((.*?)\)", source, re.DOTALL):
        method = match.group(1).upper()
        raw_path = match.group(2).strip()
        if not raw_path:
            path = ""
        elif raw_path.startswith("["):
            # None of the CLI endpoints use multi-route decorators. Refuse to
            # guess if that changes.
            continue
        else:
            path = _quoted_argument(raw_path)
        handler_match = re.search(
            r"^\s{2}(?:async\s+)?([A-Za-z_]\w*)\s*\(",
            source[match.end() :],
            re.MULTILINE,
        )
        if not handler_match:
            raise AssertionError(f"{method} {path or '/'} has no handler")
        handler_name = handler_match.group(1)
        handler_start = match.end() + handler_match.start()
        signature = _balanced_delimited(source, handler_start, "(", ")")
        body_types = frozenset(
            re.findall(
                r"@Body\([^)]*\)\s*[A-Za-z_]\w*\s*:\s*([A-Za-z_]\w*)",
                signature,
            )
        )
        route = Route(method=method, path=_normalize_path(prefix, path))
        bindings[route] = HandlerBinding(handler_name, body_types)
    return bindings


def _balanced_block(source: str, start: int) -> str:
    return _balanced_delimited(source, start, "{", "}")


def class_fields(source: str, class_name: str) -> tuple[set[str], set[str]]:
    """Return (all fields, required fields) declared directly by a DTO class."""
    match = re.search(rf"\bexport\s+class\s+{re.escape(class_name)}\b", source)
    if not match:
        raise AssertionError(f"class {class_name} not found")
    body = _balanced_block(source, match.end())

    fields: set[str] = set()
    required: set[str] = set()
    pending_decorators: list[str] = []
    decorator_depth = 0

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("@") or decorator_depth:
            pending_decorators.append(stripped)
            decorator_depth += stripped.count("(") - stripped.count(")")
            continue

        field_match = re.match(
            r"^  ([A-Za-z_]\w*)(\?)?\s*(?::[^=;]+|=\s*[^;]+);?\s*$",
            line,
        )
        if field_match:
            name, question_mark = field_match.groups()
            fields.add(name)
            decorators = " ".join(pending_decorators)
            has_initializer = "=" in line
            if question_mark is None and "@IsOptional" not in decorators and not has_initializer:
                required.add(name)
        pending_decorators = []
        decorator_depth = 0

    return fields, required


def interface_fields(source: str, interface_name: str) -> tuple[set[str], set[str]]:
    match = re.search(rf"\bexport\s+interface\s+{re.escape(interface_name)}\b", source)
    if not match:
        raise AssertionError(f"interface {interface_name} not found")
    body = _balanced_block(source, match.end())
    fields: set[str] = set()
    required: set[str] = set()
    for line in body.splitlines():
        field_match = re.match(r"^\s*([A-Za-z_]\w*)(\?)?\s*:", line)
        if not field_match:
            continue
        name, question_mark = field_match.groups()
        fields.add(name)
        if question_mark is None:
            required.add(name)
    return fields, required


def handler_multipart_fields(
    source: str,
    handler_name: str,
) -> tuple[set[str], set[str]]:
    match = re.search(
        rf"\b(?:async\s+)?{re.escape(handler_name)}\s*\(",
        source,
    )
    if not match:
        raise AssertionError(f"handler {handler_name} not found")
    body = _balanced_block(source, match.end())
    fields = set(re.findall(r"file\.fields\?\.([A-Za-z_]\w*)", body))
    required = set(re.findall(r"if\s*\(\s*!([A-Za-z_]\w*)\s*\)", body))
    required.update(
        re.findall(
            r"if\s*\(\s*![A-Za-z_]\w*\.includes\(([A-Za-z_]\w*)\)",
            body,
        )
    )
    return fields, required & fields


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    return json.loads(path.read_text())


def check_contract(contract: dict[str, Any], docmost_repo: Path) -> list[str]:
    errors: list[str] = []
    operations = contract["operations"]

    for operation_name, operation in operations.items():
        upstream = operation["upstream"]
        source_path = docmost_repo / upstream["file"]
        try:
            source = _read(source_path)
            if upstream["kind"] == "controller":
                route = Route(operation["method"], operation["path"])
                bindings = controller_bindings(source)
                if route not in bindings:
                    errors.append(
                        f"{operation_name}: {route.method} {route.path} is absent from "
                        f"{upstream['file']}"
                    )
                else:
                    binding = bindings[route]
                    expected_handler = upstream["handler"]
                    if binding.name != expected_handler:
                        errors.append(
                            f"{operation_name}: route handler changed; expected "
                            f"{expected_handler}, got {binding.name}"
                        )
                    expected_body_types = frozenset(upstream.get("body_types", []))
                    if binding.body_types != expected_body_types:
                        errors.append(
                            f"{operation_name}: handler body types changed; expected "
                            f"{sorted(expected_body_types)}, got "
                            f"{sorted(binding.body_types)}"
                        )
            elif upstream["kind"] == "client-reference":
                route_literal = upstream["route_literal"]
                if route_literal not in source:
                    errors.append(
                        f"{operation_name}: {route_literal!r} is absent from {upstream['file']}"
                    )
            else:
                errors.append(f"{operation_name}: unsupported upstream kind {upstream['kind']!r}")
        except AssertionError as exc:
            errors.append(f"{operation_name}: {exc}")
            continue

        schema_fields: set[str] = set()
        schema_required: set[str] = set()
        for schema in operation.get("schema_sources", []):
            try:
                schema_source = _read(docmost_repo / schema["file"])
                if schema["kind"] == "class":
                    actual_fields, actual_required = class_fields(
                        schema_source,
                        schema["name"],
                    )
                elif schema["kind"] == "interface":
                    actual_fields, actual_required = interface_fields(
                        schema_source,
                        schema["name"],
                    )
                elif schema["kind"] == "multipart-handler":
                    actual_fields, actual_required = handler_multipart_fields(
                        schema_source,
                        schema["name"],
                    )
                else:
                    raise AssertionError(f"unsupported schema source kind {schema['kind']!r}")
            except AssertionError as exc:
                errors.append(f"{operation_name}: {exc}")
                continue

            expected_fields = set(schema["fields"])
            expected_required = set(schema.get("required", []))
            if actual_fields != expected_fields:
                errors.append(
                    f"{operation_name}: {schema['name']} fields changed; "
                    f"expected {sorted(expected_fields)}, got {sorted(actual_fields)}"
                )
            if actual_required != expected_required:
                errors.append(
                    f"{operation_name}: {schema['name']} required fields changed; "
                    f"expected {sorted(expected_required)}, got {sorted(actual_required)}"
                )

            schema_fields.update(actual_fields)
            if not schema.get("all_optional", False):
                schema_required.update(actual_required)

        allowed_fields = set(operation.get("allowed_fields", []))
        required_fields = set(operation.get("required_fields", []))
        has_schema_sources = bool(operation.get("schema_sources"))
        if has_schema_sources and schema_fields != allowed_fields:
            errors.append(
                f"{operation_name}: contract allowed_fields do not match its schema "
                f"sources; expected {sorted(schema_fields)}, got {sorted(allowed_fields)}"
            )
        if has_schema_sources and schema_required != required_fields:
            errors.append(
                f"{operation_name}: contract required_fields do not match its schema "
                f"sources; expected {sorted(schema_required)}, got {sorted(required_fields)}"
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docmost-repo",
        type=Path,
        required=True,
        help="Path to a Docmost source checkout",
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=DEFAULT_CONTRACT,
        help="Pinned contract JSON (default: %(default)s)",
    )
    args = parser.parse_args()

    docmost_repo = args.docmost_repo.resolve()
    contract = load_contract(args.contract)
    errors = check_contract(contract, docmost_repo)
    if errors:
        print("Docmost contract drift detected:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Contract matches {contract['docmost']['repository']}@{contract['docmost']['ref']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
