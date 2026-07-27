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


def _route_decorator_paths(raw: str) -> list[str]:
    if not raw:
        return [""]
    if not raw.startswith("["):
        return [_quoted_argument(raw)]
    if not raw.endswith("]"):
        raise AssertionError(f"unsupported route decorator argument: {raw!r}")

    members = raw[1:-1]
    matches = list(re.finditer(r"(['\"])(.*?)\1", members, re.DOTALL))
    remainder = list(members)
    for match in matches:
        remainder[match.start() : match.end()] = " " * (match.end() - match.start())
    if not matches or not re.fullmatch(r"[\s,]*", "".join(remainder)):
        raise AssertionError(f"unsupported route decorator argument: {raw!r}")
    return [match.group(2) for match in matches]


def _typescript_code_mask(source: str) -> list[bool]:
    """Mark offsets that are TypeScript code rather than strings or comments."""
    mask = [False] * len(source)
    state = "code"
    index = 0
    while index < len(source):
        current = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            mask[index] = True
            if current == "/" and following == "/":
                state = "line-comment"
            elif current == "/" and following == "*":
                state = "block-comment"
            elif current == "'":
                state = "single-quote"
            elif current == '"':
                state = "double-quote"
            elif current == "`":
                state = "template"
        elif state == "line-comment":
            if current == "\n":
                state = "code"
        elif state == "block-comment":
            if current == "*" and following == "/":
                state = "code"
        elif current == "\\":
            index += 2
            continue
        elif (
            (state == "single-quote" and current == "'")
            or (state == "double-quote" and current == '"')
            or (state == "template" and current == "`")
        ):
            state = "code"
        index += 1
    return mask


def _code_matches(pattern: str | re.Pattern[str], source: str) -> list[re.Match[str]]:
    code_mask = _typescript_code_mask(source)
    return [match for match in re.finditer(pattern, source) if code_mask[match.start()]]


def _balanced_delimited_bounds(
    source: str,
    start: int,
    opening: str,
    closing: str,
) -> tuple[int, int]:
    code_mask = _typescript_code_mask(source)
    opening_index = next(
        (
            index
            for index in range(start, len(source))
            if code_mask[index] and source[index] == opening
        ),
        None,
    )
    if opening_index is None:
        raise AssertionError(f"declaration has no opening {opening}")
    depth = 0
    for index in range(opening_index, len(source)):
        if not code_mask[index]:
            continue
        character = source[index]
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return opening_index, index
    raise AssertionError(f"declaration has no closing {closing}")


def _balanced_delimited(source: str, start: int, opening: str, closing: str) -> str:
    opening_index, closing_index = _balanced_delimited_bounds(
        source,
        start,
        opening,
        closing,
    )
    return source[opening_index + 1 : closing_index]


def _handler_after_route(source: str, start: int) -> tuple[str, int]:
    """Return the immediately decorated class method name and start offset."""
    cursor = start
    while True:
        whitespace = re.match(r"\s*", source[cursor:])
        assert whitespace is not None
        cursor += whitespace.end()
        if cursor >= len(source) or source[cursor] != "@":
            break
        decorator = re.match(r"@[A-Za-z_][\w.]*", source[cursor:])
        if decorator is None:
            raise AssertionError("route is followed by an unsupported decorator")
        cursor += decorator.end()
        whitespace = re.match(r"\s*", source[cursor:])
        assert whitespace is not None
        cursor += whitespace.end()
        if cursor < len(source) and source[cursor] == "(":
            _, closing_index = _balanced_delimited_bounds(source, cursor, "(", ")")
            cursor = closing_index + 1

    handler_match = re.match(
        r"(?:(?:public|private|protected|static)\s+)*(?:async\s+)?"
        r"([A-Za-z_]\w*)\s*\(",
        source[cursor:],
    )
    if handler_match is None:
        raise AssertionError("route is not immediately followed by a class method")
    return handler_match.group(1), cursor + handler_match.start()


def controller_bindings(source: str) -> dict[Route, HandlerBinding]:
    """Extract simple NestJS GET/POST routes and their handler body types."""
    controller_matches = _code_matches(
        re.compile(r"@Controller\((.*?)\)", re.DOTALL),
        source,
    )
    if not controller_matches:
        raise AssertionError("controller source has no @Controller decorator")
    controller_match = controller_matches[0]
    raw_prefix = controller_match.group(1).strip()
    prefix = _quoted_argument(raw_prefix) if raw_prefix else ""

    bindings: dict[Route, HandlerBinding] = {}
    for match in _code_matches(re.compile(r"@(Get|Post)\((.*?)\)", re.DOTALL), source):
        method = match.group(1).upper()
        raw_path = match.group(2).strip()
        paths = _route_decorator_paths(raw_path)
        try:
            handler_name, handler_start = _handler_after_route(source, match.end())
        except AssertionError as exc:
            raise AssertionError(f"{method} {paths[0] or '/'}: {exc}") from exc
        signature = _balanced_delimited(source, handler_start, "(", ")")
        body_types = frozenset(
            re.findall(
                r"@Body\([^)]*\)\s*[A-Za-z_]\w*\s*:\s*([A-Za-z_]\w*)",
                signature,
            )
        )
        for path in paths:
            route = Route(method=method, path=_normalize_path(prefix, path))
            bindings[route] = HandlerBinding(handler_name, body_types)
    return bindings


def controller_routes(source: str) -> set[Route]:
    """Extract literal GET/POST routes without parsing handler signatures."""
    controller_matches = _code_matches(
        re.compile(r"@Controller\((.*?)\)", re.DOTALL),
        source,
    )
    if not controller_matches:
        raise AssertionError("controller source has no @Controller decorator")
    controller_match = controller_matches[0]
    raw_prefix = controller_match.group(1).strip()
    prefix = _quoted_argument(raw_prefix) if raw_prefix else ""

    routes: set[Route] = set()
    for match in _code_matches(re.compile(r"@(Get|Post)\((.*?)\)", re.DOTALL), source):
        raw_path = match.group(2).strip()
        for path in _route_decorator_paths(raw_path):
            routes.add(
                Route(
                    method=match.group(1).upper(),
                    path=_normalize_path(prefix, path),
                )
            )
    return routes


def client_reference_routes(source: str) -> set[Route]:
    """Extract literal API method/path pairs from the Docmost web client."""
    pattern = re.compile(
        r"\bapi\.(get|post)\s*(?:<[^;\n]+?>)?\s*\(\s*(['\"])(/[^'\"]+)\2",
        re.IGNORECASE,
    )
    return {
        Route(match.group(1).upper(), match.group(3)) for match in _code_matches(pattern, source)
    }


def _enclosing_function_bounds(source: str, position: int) -> tuple[int, int]:
    candidates: list[tuple[int, int]] = []
    for match in _code_matches(
        re.compile(r"\bfunction\s+[A-Za-z_]\w*\s*\("),
        source,
    ):
        if match.start() >= position:
            continue
        try:
            _, signature_end = _balanced_delimited_bounds(source, match.end() - 1, "(", ")")
            body_start, body_end = _balanced_delimited_bounds(
                source,
                signature_end + 1,
                "{",
                "}",
            )
        except AssertionError:
            continue
        if body_start < position < body_end:
            candidates.append((body_start, body_end))
    if not candidates:
        raise AssertionError("multipart request is not inside a supported function")
    return max(candidates, key=lambda bounds: bounds[0])


def client_multipart_fields(source: str, route: Route) -> set[str]:
    """Extract FormData fields sent by the web client for one literal route."""
    method = re.escape(route.method.lower())
    path = re.escape(route.path)
    request_pattern = re.compile(
        rf"\bapi\.{method}\s*(?:<[^;\n]+?>)?\s*\(\s*"
        rf"(['\"]){path}\1\s*,\s*([A-Za-z_]\w*)",
        re.IGNORECASE,
    )
    matches = _code_matches(request_pattern, source)
    if len(matches) != 1:
        raise AssertionError(
            f"expected one multipart client request for {route.method} {route.path}, "
            f"found {len(matches)}"
        )

    request = matches[0]
    form_name = request.group(2)
    function_start, _ = _enclosing_function_bounds(source, request.start())
    request_scope = source[function_start + 1 : request.start()]
    declarations = _code_matches(
        re.compile(rf"\b(?:const|let)\s+{re.escape(form_name)}\s*=\s*new\s+FormData\(\s*\)"),
        request_scope,
    )
    if not declarations:
        raise AssertionError(
            f"multipart request for {route.method} {route.path} has no FormData declaration"
        )
    form_source = request_scope[declarations[-1].end() :]
    field_pattern = re.compile(rf"\b{re.escape(form_name)}\.(?:append|set)\(\s*(['\"])(.*?)\1")
    return {match.group(2) for match in _code_matches(field_pattern, form_source)}


def _balanced_block(source: str, start: int) -> str:
    return _balanced_delimited(source, start, "{", "}")


def _top_level_class_lines(body: str) -> list[str]:
    code_mask = _typescript_code_mask(body)
    lines: list[str] = []
    depth = 0
    offset = 0
    for raw_line in body.splitlines(keepends=True):
        depth_before = depth
        for index in range(offset, offset + len(raw_line)):
            if not code_mask[index]:
                continue
            if body[index] == "{":
                depth += 1
            elif body[index] == "}":
                depth -= 1
        if depth_before == 0 or depth == 0:
            lines.append(raw_line.rstrip("\r\n"))
        offset += len(raw_line)
    return lines


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

    for line in _top_level_class_lines(body):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("@") or decorator_depth:
            pending_decorators.append(stripped)
            decorator_depth += stripped.count("(") - stripped.count(")")
            continue

        field_match = re.match(
            r"^\s*(?:(?:public|private|protected|readonly|declare|static|abstract)\s+)*"
            r"([A-Za-z_]\w*)([?!])?\s*(?::[^=;]+|=\s*[^;]+);?\s*$",
            line,
        )
        if field_match:
            name, _declaration_marker = field_match.groups()
            fields.add(name)
            decorators = " ".join(pending_decorators)
            has_initializer = "=" in line
            conditionally_validated = "@ValidateIf" in decorators
            if (
                "@IsOptional" not in decorators
                and not conditionally_validated
                and not has_initializer
            ):
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


def class_inherits(
    source: str,
    class_name: str,
    base_name: str,
    *,
    via: str | None = None,
) -> bool:
    """Return whether a DTO class extends the declared base relationship."""
    if via is None:
        expression = re.escape(base_name)
    else:
        expression = rf"{re.escape(via)}\(\s*{re.escape(base_name)}\s*\)"
    return (
        re.search(
            rf"\bexport\s+class\s+{re.escape(class_name)}\s+extends\s+"
            rf"{expression}\s*\{{",
            source,
        )
        is not None
    )


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


def _controller_route_sources(docmost_repo: Path, route: Route) -> list[Path]:
    controller_root = docmost_repo / "apps" / "server" / "src"
    if not controller_root.is_dir():
        raise AssertionError(f"missing upstream controller tree: {controller_root}")

    matches: list[Path] = []
    for source_path in controller_root.rglob("*.controller.ts"):
        source = _read(source_path)
        if route in controller_routes(source):
            matches.append(source_path.relative_to(docmost_repo))
    return matches


def check_contract(contract: dict[str, Any], docmost_repo: Path) -> list[str]:
    errors: list[str] = []
    operations = contract["operations"]

    for operation_name, operation in operations.items():
        upstream = operation["upstream"]
        source_path = docmost_repo / upstream["file"]
        bound_body_types: frozenset[str] = frozenset()
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
                    bound_body_types = binding.body_types
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
                route = Route(operation["method"], operation["path"])
                if route not in client_reference_routes(source):
                    errors.append(
                        f"{operation_name}: {route.method} {route.path} is absent from "
                        f"{upstream['file']}"
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

            inheritance = schema.get("inheritance")
            if schema.get("all_optional", False) and inheritance is None:
                errors.append(
                    f"{operation_name}: all_optional schema {schema['name']} must "
                    "declare its inheritance relationship"
                )
            if inheritance is not None:
                try:
                    inheritance_source = _read(docmost_repo / inheritance["file"])
                    relationship_exists = class_inherits(
                        inheritance_source,
                        inheritance["class"],
                        schema["name"],
                        via=inheritance.get("via"),
                    )
                except AssertionError as exc:
                    errors.append(f"{operation_name}: {exc}")
                else:
                    if not relationship_exists:
                        via = (
                            f"{inheritance['via']}({schema['name']})"
                            if inheritance.get("via")
                            else schema["name"]
                        )
                        errors.append(
                            f"{operation_name}: {inheritance['class']} no longer extends {via}"
                        )

            schema_fields.update(actual_fields)
            if not schema.get("all_optional", False):
                schema_required.update(actual_required)

        allowed_fields = set(operation.get("allowed_fields", []))
        required_fields = set(operation.get("required_fields", []))
        has_schema_sources = bool(operation.get("schema_sources"))
        if bound_body_types and not has_schema_sources:
            errors.append(
                f"{operation_name}: controller body types "
                f"{sorted(bound_body_types)} require schema_sources"
            )
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

        multipart_client = operation.get("multipart_client")
        file_fields = set(operation.get("file_fields", []))
        if file_fields and multipart_client is None:
            errors.append(f"{operation_name}: file_fields require a multipart_client source")
        if multipart_client is not None:
            try:
                multipart_source = _read(docmost_repo / multipart_client["file"])
                upstream_form_fields = client_multipart_fields(
                    multipart_source,
                    Route(operation["method"], operation["path"]),
                )
            except AssertionError as exc:
                errors.append(f"{operation_name}: {exc}")
            else:
                upstream_file_fields = upstream_form_fields - allowed_fields
                if upstream_file_fields != file_fields:
                    errors.append(
                        f"{operation_name}: multipart file fields changed; expected "
                        f"{sorted(file_fields)}, got {sorted(upstream_file_fields)}"
                    )

    for entry in contract["known_drift"]:
        if entry["kind"] != "endpoint":
            continue
        route = Route(entry["method"], entry["path"])
        absence_sources = entry.get("upstream_absence", [])
        if not absence_sources:
            errors.append(
                f"known drift {route.method} {route.path}: missing upstream_absence source"
            )
            continue
        controller_absence = False
        for upstream in absence_sources:
            try:
                if upstream["kind"] == "controller":
                    _read(docmost_repo / upstream["file"])
                    controller_absence = True
                    continue
                elif upstream["kind"] == "client-reference":
                    source = _read(docmost_repo / upstream["file"])
                    route_exists = route in client_reference_routes(source)
                else:
                    raise AssertionError(f"unsupported upstream absence kind {upstream['kind']!r}")
            except AssertionError as exc:
                errors.append(f"known drift {route.method} {route.path}: {exc}")
                continue
            if route_exists:
                errors.append(
                    f"known drift {route.method} {route.path}: endpoint now exists in "
                    f"{upstream['file']}; remove the allowlist entry"
                )

        if controller_absence:
            try:
                route_sources = _controller_route_sources(docmost_repo, route)
            except AssertionError as exc:
                errors.append(f"known drift {route.method} {route.path}: {exc}")
            else:
                if route_sources:
                    locations = ", ".join(str(path) for path in route_sources)
                    errors.append(
                        f"known drift {route.method} {route.path}: endpoint now exists "
                        f"in {locations}; remove the allowlist entry"
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
