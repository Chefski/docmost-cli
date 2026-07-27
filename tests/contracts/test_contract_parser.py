"""Focused regression tests for the lightweight TypeScript contract parser."""

from pathlib import Path

from scripts.check_docmost_contracts import (
    HandlerBinding,
    Route,
    check_contract,
    class_fields,
    class_inherits,
    client_multipart_fields,
    client_reference_routes,
    controller_bindings,
    controller_routes,
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


def test_controller_routes_do_not_require_a_parseable_handler() -> None:
    source = """
@Controller('pages')
export class PageController {
  @Post('/content')
  @UseInterceptors(ExampleInterceptor)
  handler = () => {};
}
"""
    assert controller_routes(source) == {Route("POST", "/pages/content")}


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


def test_dto_inheritance_requires_the_declared_wrapper_and_base() -> None:
    source = """
export class UpdateSpaceDto extends PartialType(CreateSpaceDto) {}
"""
    assert class_inherits(
        source,
        "UpdateSpaceDto",
        "CreateSpaceDto",
        via="PartialType",
    )
    assert not class_inherits(
        source,
        "UpdateSpaceDto",
        "OtherSpaceDto",
        via="PartialType",
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


def test_multipart_client_fields_are_scoped_to_the_requested_route() -> None:
    source = """
export async function importPage(file: File, spaceId: string) {
  const formData = new FormData();
  formData.append("spaceId", spaceId);
  formData.append("document", file);
  return api.post<IPage>("/pages/import", formData);
}

export async function uploadImage(image: File) {
  const formData = new FormData();
  formData.append("image", image);
  return api.post("/files/upload-image", formData);
}
"""
    assert client_multipart_fields(
        source,
        Route("POST", "/pages/import"),
    ) == {"document", "spaceId"}


def test_contract_check_rejects_a_renamed_multipart_file_field(tmp_path: Path) -> None:
    controller = tmp_path / "controller.ts"
    controller.write_text(
        """
@Controller()
export class ImportController {
  @Post('pages/import')
  async importPage() {}
}
"""
    )
    client = tmp_path / "client.ts"
    client.write_text(
        """
const formData = new FormData();
formData.append("spaceId", spaceId);
formData.append("document", file);
api.post("/pages/import", formData);
"""
    )
    contract = {
        "operations": {
            "pages.import": {
                "method": "POST",
                "path": "/pages/import",
                "allowed_fields": ["spaceId"],
                "required_fields": [],
                "file_fields": ["file"],
                "multipart_client": {"file": "client.ts"},
                "upstream": {
                    "kind": "controller",
                    "file": "controller.ts",
                    "handler": "importPage",
                    "body_types": [],
                },
            }
        },
        "known_drift": [],
    }

    assert check_contract(contract, tmp_path) == [
        "pages.import: multipart file fields changed; expected ['file'], got ['document']"
    ]


def test_known_endpoint_drift_searches_the_whole_controller_tree(tmp_path: Path) -> None:
    controller_root = tmp_path / "apps" / "server" / "src"
    configured = controller_root / "core" / "page" / "page.controller.ts"
    configured.parent.mkdir(parents=True)
    configured.write_text(
        """
@Controller('pages')
export class PageController {}
"""
    )
    moved = controller_root / "other" / "moved.controller.ts"
    moved.parent.mkdir(parents=True)
    moved.write_text(
        """
@Controller('pages')
export class MovedController {
  @Post('content')
  async content() {}
}
"""
    )
    contract = {
        "operations": {},
        "known_drift": [
            {
                "kind": "endpoint",
                "method": "POST",
                "path": "/pages/content",
                "upstream_absence": [
                    {
                        "kind": "controller",
                        "file": str(configured.relative_to(tmp_path)),
                    }
                ],
            }
        ],
    }

    errors = check_contract(contract, tmp_path)
    assert len(errors) == 1
    assert "apps/server/src/other/moved.controller.ts" in errors[0]


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
