# Contract and integration testing

The regular unit tests use HTTP mocks. Those tests are fast, but a mock can accidentally describe
an endpoint that Docmost does not expose. The contract suite adds two independent checks:

1. `tests/contracts/docmost-v0.95.0.json` records the exact controller route and DTO fields for
   every API operation used by the CLI. It is pinned to a specific Docmost commit.
2. `scripts/check_docmost_contracts.py` reads those controllers and DTOs from a Docmost checkout.
   It fails when a route, DTO field, required field, multipart field, or Enterprise client
   reference drifts.

The CLI-source inventory test rejects every literal API call that is neither in the pinned
contract nor in the small `known_drift` list. Known drift is intentionally visible, assigned to
an owning fix, and tested for staleness; adding a new nonexistent endpoint cannot silently pass.

## Local and CI drift checks

Check a sibling Docmost checkout:

```bash
python scripts/check_docmost_contracts.py --docmost-repo ../docmost
pytest tests/contracts
```

CI checks out the pinned Docmost commit with a sparse checkout and runs both commands. When
updating the pin, review the upstream controller and DTO changes, update the JSON snapshot, and
remove any `known_drift` entries whose owning fix has landed.

## Real-instance tests

Real-instance tests always skip unless `--run-docmost-integration` is passed. Supplying
credentials alone is not enough. Community authentication uses email and password; Enterprise
may use an API key.

Tests that depend on an operation in `known_drift` also skip with the owning fix in the reason.
Removing a resolved allowlist entry automatically activates its live coverage.

Read-only Community example:

```bash
export DOCMOST_INTEGRATION_URL="https://docmost-test.example.com"
export DOCMOST_INTEGRATION_EMAIL="cli-tests@example.com"
export DOCMOST_INTEGRATION_PASSWORD="..."
export DOCMOST_INTEGRATION_SPACE_ID="<readable test space UUID>"
export DOCMOST_INTEGRATION_PAGE_ID="<optional readable page UUID>"
export DOCMOST_INTEGRATION_ATTACHMENT_ID="<optional readable attachment UUID>"

pytest tests/integration --run-docmost-integration
```

The workspace-member check requires an authorized account and
`DOCMOST_INTEGRATION_ALLOW_ADMIN_READS=1`.

Enterprise attachment search is tested only when both of these are set:

```bash
export DOCMOST_INTEGRATION_EDITION="enterprise"
export DOCMOST_INTEGRATION_ATTACHMENT_SEARCH=1
```

Use `DOCMOST_INTEGRATION_API_KEY` instead of email/password when testing API-key authentication.

## Mutation safety and cleanup

Mutation tests require two independent, explicit settings:

```bash
export DOCMOST_INTEGRATION_ALLOW_MUTATIONS=1
export DOCMOST_INTEGRATION_MUTATION_SPACE_ID="<dedicated disposable space UUID>"

pytest tests/integration --run-docmost-integration
```

Never point `DOCMOST_INTEGRATION_MUTATION_SPACE_ID` at a production or shared space. The test
records only page IDs returned by that run and deletes those pages during teardown. It first asks
for permanent deletion and falls back to trashing when the account lacks that permission.
Attachments and comments owned by a deleted test page follow the server's page cleanup behavior.
If the process is killed, search the dedicated space for titles beginning with `Contract` or
`CLI contract` and remove them manually.

Cross-space copy and move additionally require a second dedicated disposable space:

```bash
export DOCMOST_INTEGRATION_SECOND_MUTATION_SPACE_ID="<second test space UUID>"
```

Workspace-level space creation is separately gated by
`DOCMOST_INTEGRATION_ALLOW_SPACE_MUTATIONS=1`. Created space IDs are deleted during teardown.

ZIP import is asynchronous and can outlive test teardown. It is therefore disabled unless
`DOCMOST_INTEGRATION_ALLOW_ASYNC_IMPORT=1` is also set. Run it only against a disposable space,
then wait for the file task to finish and manually remove imported pages if the test instance
does not automatically clean them up.
