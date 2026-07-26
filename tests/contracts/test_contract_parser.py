"""Focused regression tests for the lightweight TypeScript contract parser."""

from scripts.check_docmost_contracts import (
    HandlerBinding,
    Route,
    class_fields,
    client_reference_routes,
    controller_bindings,
    handler_multipart_fields,
)


def test_controller_route_is_bound_to_handler_body_types() -> None:
    source = """
@Controller('pages')
export class PageController {
  @Post('/history')
  async getPageHistory(
    @Body() dto: PageIdDto,
    @Body()
    pagination: PaginationOptions,
  ) {}
}
"""
    assert controller_bindings(source) == {
        Route("POST", "/pages/history"): HandlerBinding(
            "getPageHistory",
            frozenset({"PageIdDto", "PaginationOptions"}),
        )
    }


def test_dto_initializer_is_not_required() -> None:
    source = """
export class ExampleDto {
  enabled = true;

  requiredValue: string;
}
"""
    assert class_fields(source, "ExampleDto") == (
        {"enabled", "requiredValue"},
        {"requiredValue"},
    )


def test_client_reference_route_includes_http_method() -> None:
    source = """
const req = await api.post<{ items: Result[] }>("/search-attachments", params);
await api.get('/search-attachments');
"""
    assert client_reference_routes(source) == {
        Route("GET", "/search-attachments"),
        Route("POST", "/search-attachments"),
    }


def test_multipart_requiredness_comes_from_handler_validation() -> None:
    source = """
export class ImportController {
  async importZip() {
    const spaceId = file.fields?.spaceId?.value;
    const source = file.fields?.source?.value;
    const note = file.fields?.note?.value;
    if (!spaceId) {
      throw new Error('spaceId is required');
    }
    if (!validSources.includes(source)) {
      throw new Error('source is required');
    }
  }
}
"""
    assert handler_multipart_fields(source, "importZip") == (
        {"note", "source", "spaceId"},
        {"source", "spaceId"},
    )
