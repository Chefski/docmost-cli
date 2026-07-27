"""DocmostClient: central HTTP client with auth, retry, and error handling.

All API calls go through this client. It handles:
- Auth header injection via AuthStrategy
- 401 retry (session auth re-authentication)
- Exponential backoff retry for transient failures on replay-safe requests
- HTTP error translation to user-friendly messages with exit codes
- Optional verbose debug logging
"""

import logging
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import quote

import httpx

from docmost_cli.api.auth import AuthError, AuthStrategy, create_auth
from docmost_cli.config.settings import DocmostSettings
from docmost_cli.output.formatter import print_error

__all__ = ["DocmostClient"]

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0
_BACKOFF_FACTOR = 2.0
_MAX_RETRY_AFTER = 60.0
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


class DocmostClient:
    """HTTP client for the Docmost API.

    Uses httpx.Client for connection pooling. Provides authenticated
    request methods with automatic error handling and mutation-safe retry logic.
    """

    def __init__(self, settings: DocmostSettings, *, verbose: bool = False) -> None:
        if not settings.url:
            print_error(
                "No Docmost URL configured. Run 'docmost-cli config init' or set DOCMOST_URL.",
                exit_code=1,
            )

        self._settings = settings
        self._base_url = settings.url.rstrip("/")
        self._auth: AuthStrategy = create_auth(settings)
        self._http = httpx.Client(timeout=30.0)
        self._verbose = verbose

        # Set up logging
        self._log = logging.getLogger("docmost_cli")
        if verbose and not self._log.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("[docmost] %(message)s"))
            self._log.addHandler(handler)
            self._log.setLevel(logging.DEBUG)

    def _send_with_retry(
        self,
        request_factory: Callable[[], httpx.Request],
        *,
        retry_safe: bool = False,
        error_messages: Mapping[int, str] | None = None,
        body_replayable: bool = True,
    ) -> httpx.Response:
        """Send a request with authentication and mutation-safe retry handling.

        Transient transport errors, HTTP 429 responses, and HTTP 5xx responses
        are retried only for idempotent HTTP methods or when the caller
        explicitly marks a request as safe to replay. A single replay of a
        replayable body after a session-auth 401 is allowed for every method
        because the rejected request was not authorized.

        Args:
            request_factory: Creates a fresh, complete request for every attempt.
            retry_safe: Whether the caller guarantees the request is safe to replay.
            error_messages: Optional status-specific error messages.
            body_replayable: Whether rebuilding reproduces the complete request body.

        Returns:
            The HTTP response (success only; errors raise SystemExit).
        """
        # Rebuilding from source arguments lets httpx rewind seekable multipart
        # fields without retaining a second encoded copy of a large upload.
        request = request_factory()
        can_retry_transient = body_replayable and (
            retry_safe or request.method.upper() in _IDEMPOTENT_METHODS
        )
        transient_attempt = 0
        reauthenticated = False

        while True:
            self._auth.apply(request)

            if self._verbose and transient_attempt == 0 and not reauthenticated:
                self._log.debug("%s %s", request.method, request.url)

            start = time.monotonic()

            try:
                response = self._http.send(request)
            except httpx.TransportError as exc:
                if can_retry_transient and transient_attempt < _MAX_RETRIES:
                    wait = _BASE_BACKOFF * (_BACKOFF_FACTOR**transient_attempt)
                    transient_attempt += 1
                    self._log_retry(wait, transient_attempt)
                    time.sleep(wait)
                    request = request_factory()
                    continue
                self._handle_transport_error(
                    exc,
                    method=request.method,
                    retry_skipped=not can_retry_transient,
                )

            if self._verbose:
                elapsed = (time.monotonic() - start) * 1000
                suffix = " (retry)" if transient_attempt or reauthenticated else ""
                self._log.debug("  → %s (%dms)%s", response.status_code, elapsed, suffix)

            # A 401 means the server rejected the request before authorizing the
            # operation, so one replay after refreshing session auth is safe
            # even for a mutation.
            if response.status_code == 401 and self._auth.can_retry() and not body_replayable:
                print_error(
                    "Session expired, but this request body cannot be replayed safely. "
                    "Retry with bytes, a list or tuple of byte chunks, or seekable "
                    "multipart files.",
                    exit_code=3,
                )
            if (
                response.status_code == 401
                and self._auth.can_retry()
                and not reauthenticated
                and body_replayable
            ):
                response.close()
                try:
                    self._auth.refresh(self._http)
                except AuthError as exc:
                    print_error(str(exc), exit_code=3)
                reauthenticated = True
                request = request_factory()
                continue

            if (
                response.status_code in _RETRYABLE_STATUS
                and can_retry_transient
                and transient_attempt < _MAX_RETRIES
            ):
                wait = self._retry_delay(response, transient_attempt)
                transient_attempt += 1
                response.close()
                self._log_retry(wait, transient_attempt)
                time.sleep(wait)
                request = request_factory()
                continue

            self._handle_error(
                response,
                method=request.method,
                retry_skipped=(
                    response.status_code in _RETRYABLE_STATUS and not can_retry_transient
                ),
                error_messages=error_messages,
            )
            return response

    def _log_retry(self, wait: float, attempt: int) -> None:
        """Log a retry delay when verbose output is enabled."""
        if self._verbose:
            self._log.debug(
                "  Retrying in %.1fs (attempt %d/%d)...",
                wait,
                attempt,
                _MAX_RETRIES,
            )

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        """Return the server-requested or exponential retry delay."""
        default = _BASE_BACKOFF * (_BACKOFF_FACTOR**attempt)
        retry_after = response.headers.get("Retry-After")
        if not retry_after:
            return default

        value = retry_after.strip()
        if value.isdigit():
            return min(float(value), _MAX_RETRY_AFTER)

        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return default
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        delay = max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        return min(delay, _MAX_RETRY_AFTER)

    def _handle_transport_error(
        self,
        exc: httpx.TransportError,
        *,
        method: str,
        retry_skipped: bool,
    ) -> None:
        """Translate transport failures, including ambiguous mutation outcomes."""
        if isinstance(exc, httpx.ConnectError):
            message = f"Cannot connect to {self._base_url}. Check the URL and your network."
        elif isinstance(exc, httpx.TimeoutException):
            message = f"Request timed out contacting {self._base_url}."
        else:
            message = f"Network error contacting {self._base_url}: {exc}"

        if retry_skipped:
            if isinstance(exc, httpx.ConnectError):
                message += f" The {method.upper()} request was not retried automatically."
            else:
                message += (
                    f" The {method.upper()} request was not retried automatically because "
                    "its outcome may be unknown; verify server state before trying again."
                )
        print_error(message, exit_code=1)

    @staticmethod
    def _content_is_replayable(content: Any) -> bool:
        """Return whether raw content is a known-repeatable byte source."""
        if content is None or isinstance(content, (str, bytes)):
            return True
        return type(content) in (list, tuple) and all(isinstance(chunk, bytes) for chunk in content)

    @staticmethod
    def _data_is_replayable(data: Any) -> bool:
        """Return whether form or legacy streaming data can be rebuilt safely."""
        if data is None or isinstance(data, Mapping):
            return True
        return DocmostClient._content_is_replayable(data)

    @staticmethod
    def _files_are_replayable(files: Any) -> bool:
        """Return whether multipart file values are bytes or seekable streams."""
        if not files:
            return True

        if isinstance(files, Mapping):
            values: Iterable[Any] = files.values()
        elif type(files) in (list, tuple):
            if not all(type(item) in (list, tuple) and len(item) == 2 for item in files):
                return False
            values = (item[1] for item in files)
        else:
            return False

        for value in values:
            file_value = value[1] if isinstance(value, tuple) and len(value) >= 2 else value
            if isinstance(file_value, (str, bytes)):
                continue
            try:
                seekable = getattr(file_value, "seekable", None)
                if not callable(seekable) or not seekable():
                    return False
                position = file_value.tell()
                file_value.seek(0)
                if file_value.tell() != 0:
                    return False
                file_value.seek(position)
                if file_value.tell() != position:
                    return False
            except (AttributeError, OSError, TypeError, ValueError):
                return False
        return True

    def request(
        self,
        method: str,
        path: str,
        *,
        error_messages: Mapping[int, str] | None = None,
        retry_safe: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make an authenticated API request with error handling.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: API path relative to /api/ (e.g., "/pages/info").
            error_messages: Optional status-specific error messages.
            retry_safe: Explicitly allow retries for a replay-safe request.
            **kwargs: Additional arguments passed to httpx (json, params, etc.).

        Returns:
            Parsed JSON response body.
        """
        url = self.api_url(path)

        def request_factory() -> httpx.Request:
            return self._http.build_request(method, url, **kwargs)

        body_replayable = self._content_is_replayable(kwargs.get("content"))
        body_replayable = body_replayable and self._data_is_replayable(kwargs.get("data"))
        files = kwargs.get("files")
        if files is not None:
            body_replayable = body_replayable and self._files_are_replayable(files)
        response = self._send_with_retry(
            request_factory,
            retry_safe=retry_safe,
            error_messages=error_messages,
            body_replayable=body_replayable,
        )
        return cast("dict[str, Any]", response.json())

    def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        *,
        error_messages: Mapping[int, str] | None = None,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        """Convenience method for POST requests.

        Most Docmost API endpoints use POST.

        Args:
            path: API path relative to /api/.
            json: JSON body to send.
            error_messages: Optional status-specific error messages.
            retry_safe: Explicitly allow retries when this POST is read-only or idempotent.

        Returns:
            Parsed JSON response body.
        """
        return self.request(
            "POST",
            path,
            json=json,
            retry_safe=retry_safe,
            error_messages=error_messages,
        )

    def post_multipart(
        self,
        path: str,
        data: dict[str, str] | None = None,
        files: dict[str, Any] | None = None,
        *,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        """POST with multipart/form-data for file uploads.

        Args:
            path: API path relative to /api/.
            data: Form fields.
            files: File fields (httpx files format).
            retry_safe: Explicitly allow retries for replay-safe multipart requests.

        Returns:
            Parsed JSON response body.
        """
        url = self.api_url(path)

        def request_factory() -> httpx.Request:
            return self._http.build_request("POST", url, data=data, files=files)

        response = self._send_with_retry(
            request_factory,
            retry_safe=retry_safe,
            body_replayable=self._files_are_replayable(files),
        )
        return cast("dict[str, Any]", response.json())

    def post_raw(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        *,
        raise_on_error: bool = True,
        retry_safe: bool = False,
    ) -> httpx.Response:
        """POST request returning raw httpx.Response.

        Use for binary/non-JSON responses or silent probes.

        Args:
            path: API path relative to /api/.
            json: JSON body to send.
            raise_on_error: If False, skip error handling (for endpoint probes).
            retry_safe: Explicitly allow retries for a replay-safe POST.
        """
        url = self.api_url(path)

        def request_factory() -> httpx.Request:
            return self._http.build_request("POST", url, json=json)

        if raise_on_error:
            return self._send_with_retry(request_factory, retry_safe=retry_safe)

        request = request_factory()
        self._auth.apply(request)
        try:
            return self._http.send(request)
        except httpx.HTTPError:
            return httpx.Response(status_code=0)  # Sentinel for failed probe

    def get_raw(self, path: str, *, raise_on_error: bool = True) -> httpx.Response:
        """GET a binary/non-JSON resource with authentication.

        Args:
            path: API path relative to ``/api``.
            raise_on_error: Whether to translate unsuccessful responses into CLI errors.

        Returns:
            The raw HTTP response.
        """
        url = self.api_url(path)

        def request_factory() -> httpx.Request:
            return self._http.build_request("GET", url)

        if raise_on_error:
            return self._send_with_retry(request_factory)

        request = request_factory()
        self._auth.apply(request)
        try:
            return self._http.send(request)
        except httpx.HTTPError:
            return httpx.Response(status_code=0)

    def api_url(self, path: str) -> str:
        """Build an absolute URL for an API path."""
        normalized = path if path.startswith("/") else f"/{path}"
        return f"{self._base_url}/api{normalized}"

    def attachment_url(self, attachment_id: str, file_name: str) -> str:
        """Build the stable authenticated URL for an attachment."""
        encoded_name = quote(file_name, safe="")
        return self.api_url(f"/files/{attachment_id}/{encoded_name}")

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Convenience method for GET requests.

        Args:
            path: API path relative to /api/.
            **kwargs: Additional arguments (params, etc.).

        Returns:
            Parsed JSON response body.
        """
        return self.request("GET", path, **kwargs)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self) -> "DocmostClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @staticmethod
    def _handle_error(
        response: httpx.Response,
        *,
        error_messages: Mapping[int, str] | None = None,
        method: str = "",
        retry_skipped: bool = False,
    ) -> None:
        """Translate HTTP error responses to user-friendly messages.

        Args:
            response: The HTTP response to check.
            error_messages: Optional status-specific error messages.
            method: Request method, used to explain mutation retry safety.
            retry_skipped: Whether a transient retry was deliberately skipped.
        """
        if response.is_success:
            return

        status = response.status_code

        if error_messages and status in error_messages:
            print_error(error_messages[status], exit_code=4 if status == 404 else 1)
        elif status == 401:
            print_error(
                "Authentication failed. Run 'docmost-cli config test' to verify.",
                exit_code=3,
            )
        elif status == 403:
            print_error("Permission denied.", exit_code=1)
        elif status == 404:
            print_error(
                "Resource not found. Check the ID or slug.",
                exit_code=4,
            )
        elif status == 422:
            try:
                detail = response.json().get("message", "Validation error")
            except (ValueError, AttributeError):
                detail = "Validation error"
            print_error(f"Validation error: {detail}", exit_code=1)
        elif status == 429:
            message = "Rate limited. Try again later."
            if retry_skipped:
                message = (
                    f"Rate limited. The {method.upper()} request was not retried automatically "
                    "to avoid duplicate changes; verify its result before retrying."
                )
            print_error(message, exit_code=1)
        elif status >= 500:
            message = f"Server error ({status}). Check Docmost logs."
            if retry_skipped:
                message += (
                    f" The {method.upper()} request was not retried automatically because it "
                    "may have completed; verify server state before trying again."
                )
            print_error(message, exit_code=1)
        else:
            print_error(
                f"Unexpected error (HTTP {status}).",
                exit_code=1,
            )
