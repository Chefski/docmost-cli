"""Tests for DocmostClient."""

import io
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from docmost_cli.api.client import DocmostClient
from docmost_cli.api.users import get_current_user
from docmost_cli.config.settings import DocmostSettings


class TestDocmostClient:
    def test_successful_post(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/users/me",
            json={"name": "Test User", "email": "test@example.com"},
        )
        with DocmostClient(api_key_settings) as client:
            result = client.post("/users/me")
        assert result["name"] == "Test User"

    def test_auth_header_sent(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/users/me",
            json={"name": "Test"},
        )
        with DocmostClient(api_key_settings) as client:
            client.post("/users/me")

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == "Bearer dm_test1234567890"

    def test_401_exits_with_code_3(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/users/me",
            status_code=401,
        )
        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit) as exc_info:
            client.post("/users/me")
        assert exc_info.value.code == 3

    def test_404_exits_with_code_4(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/info",
            status_code=404,
        )
        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit) as exc_info:
            client.post("/pages/info", json={"pageId": "nonexistent"})
        assert exc_info.value.code == 4

    def test_422_exits_with_message(self, httpx_mock, api_key_settings) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/create",
            status_code=422,
            json={"message": "Title is required"},
        )
        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit) as exc_info:
            client.post("/pages/create", json={})
        assert exc_info.value.code == 1

    def test_no_url_exits(self) -> None:
        settings = DocmostSettings(api_key="dm_key")
        with pytest.raises(SystemExit):
            DocmostClient(settings)

    def test_context_manager(self, api_key_settings) -> None:
        with DocmostClient(api_key_settings) as client:
            assert client is not None

    def test_verbose_mode(self, httpx_mock, api_key_settings, capfd) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/users/me",
            json={"name": "Test"},
        )
        with DocmostClient(api_key_settings, verbose=True) as client:
            client.post("/users/me")
        captured = capfd.readouterr()
        assert "POST" in captured.err
        assert "200" in captured.err


class TestMutationSafeRetries:
    def test_get_retries_5xx_then_succeeds(
        self,
        httpx_mock,
        api_key_settings,
        monkeypatch,
    ) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", sleeps.append)
        httpx_mock.add_response(
            url="https://docs.example.com/api/health",
            status_code=503,
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/health",
            json={"status": "ok"},
        )

        with DocmostClient(api_key_settings) as client:
            result = client.get("/health")

        assert result == {"status": "ok"}
        assert sleeps == [1.0]
        assert len(httpx_mock.get_requests()) == 2

    def test_explicitly_safe_post_retries_with_same_json_body(
        self,
        httpx_mock,
        api_key_settings,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        url = "https://docs.example.com/api/pages/info"
        httpx_mock.add_response(url=url, status_code=500)
        httpx_mock.add_response(url=url, json={"id": "page-1"})

        with DocmostClient(api_key_settings) as client:
            result = client.post(
                "/pages/info",
                json={"pageId": "page-1"},
                retry_safe=True,
            )

        requests = httpx_mock.get_requests()
        assert result == {"id": "page-1"}
        assert len(requests) == 2
        assert requests[0].content == requests[1].content == b'{"pageId":"page-1"}'

    def test_explicitly_safe_raw_post_retries_then_accepts_empty_response(
        self, httpx_mock, api_key_settings, monkeypatch
    ) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/move-to-space",
            status_code=503,
        )
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/move-to-space",
            status_code=200,
        )
        with DocmostClient(api_key_settings) as client:
            response = client.post_raw(
                "/pages/move-to-space",
                json={"pageId": "page-1", "spaceId": "space-2"},
                retry_safe=True,
            )
        assert response.status_code == 200
        assert len(httpx_mock.get_requests()) == 2

    def test_read_only_post_wrapper_opts_into_safe_retries(
        self,
        httpx_mock,
        api_key_settings,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        url = "https://docs.example.com/api/users/me"
        httpx_mock.add_response(url=url, status_code=503)
        httpx_mock.add_response(url=url, json={"name": "Retry User"})

        with DocmostClient(api_key_settings) as client:
            result = get_current_user(client)

        assert result["name"] == "Retry User"
        assert len(httpx_mock.get_requests()) == 2

    def test_retry_after_seconds_is_honored(
        self,
        httpx_mock,
        api_key_settings,
        monkeypatch,
    ) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", sleeps.append)
        url = "https://docs.example.com/api/health"
        httpx_mock.add_response(
            url=url,
            status_code=429,
            headers={"Retry-After": "7"},
        )
        httpx_mock.add_response(url=url, json={"status": "ok"})

        with DocmostClient(api_key_settings) as client:
            client.get("/health")

        assert sleeps == [7.0]

    def test_retry_after_http_date_is_honored_and_capped(
        self,
        httpx_mock,
        api_key_settings,
        monkeypatch,
    ) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", sleeps.append)
        retry_at = format_datetime(datetime.now(UTC) + timedelta(minutes=5), usegmt=True)
        url = "https://docs.example.com/api/health"
        httpx_mock.add_response(
            url=url,
            status_code=503,
            headers={"Retry-After": retry_at},
        )
        httpx_mock.add_response(url=url, json={"status": "ok"})

        with DocmostClient(api_key_settings) as client:
            client.get("/health")

        assert sleeps == [60.0]

    @pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
    def test_non_idempotent_post_is_not_retried(
        self,
        httpx_mock,
        api_key_settings,
        capsys,
        status_code,
    ) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/pages/create",
            status_code=status_code,
        )

        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit) as exc_info:
            client.post("/pages/create", json={"title": "Only once"})

        assert exc_info.value.code == 1
        assert len(httpx_mock.get_requests()) == 1
        error = " ".join(capsys.readouterr().err.split())
        assert "not retried automatically" in error
        assert "verify" in error

    def test_non_idempotent_multipart_5xx_is_not_retried(
        self,
        httpx_mock,
        api_key_settings,
    ) -> None:
        httpx_mock.add_response(
            url="https://docs.example.com/api/files/upload",
            status_code=500,
        )

        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit):
            client.post_multipart(
                "/files/upload",
                data={"pageId": "page-1"},
                files={"file": ("report.txt", b"one upload", "text/plain")},
            )

        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert b"one upload" in requests[0].content

    def test_get_stops_after_retry_budget(
        self,
        httpx_mock,
        api_key_settings,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        for _ in range(4):
            httpx_mock.add_response(
                url="https://docs.example.com/api/health",
                status_code=500,
            )

        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit) as exc_info:
            client.get("/health")

        assert exc_info.value.code == 1
        assert len(httpx_mock.get_requests()) == 4

    def test_one_shot_stream_body_disables_automatic_retry(
        self,
        httpx_mock,
        api_key_settings,
        capsys,
    ) -> None:
        url = "https://docs.example.com/api/pages/raw"
        httpx_mock.add_response(url=url, status_code=500)
        chunks = (chunk for chunk in (b"complete ", b"body"))

        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit):
            client.request("PUT", "/pages/raw", content=chunks)

        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert requests[0].content == b"complete body"
        error = " ".join(capsys.readouterr().err.split())
        assert "not retried automatically" in error
        assert "verify server state" in error

    def test_wrapped_one_shot_stream_disables_automatic_retry(
        self,
        httpx_mock,
        api_key_settings,
    ) -> None:
        url = "https://docs.example.com/api/pages/raw"
        httpx_mock.add_response(url=url, status_code=500)
        source = iter((b"complete ", b"body"))

        class WrappedStream:
            def __iter__(self):
                yield from source

        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit):
            client.request("PUT", "/pages/raw", content=WrappedStream())

        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert requests[0].content == b"complete body"

    def test_byte_chunk_list_retries_complete_body(
        self,
        httpx_mock,
        api_key_settings,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        url = "https://docs.example.com/api/pages/raw"
        httpx_mock.add_response(url=url, status_code=500)
        httpx_mock.add_response(url=url, json={"status": "ok"})

        with DocmostClient(api_key_settings) as client:
            result = client.request(
                "PUT",
                "/pages/raw",
                content=[b"complete ", b"body"],
            )

        requests = httpx_mock.get_requests()
        assert result == {"status": "ok"}
        assert len(requests) == 2
        assert requests[0].content == requests[1].content == b"complete body"

    def test_legacy_one_shot_data_disables_automatic_retry(
        self,
        httpx_mock,
        api_key_settings,
    ) -> None:
        url = "https://docs.example.com/api/pages/raw"
        httpx_mock.add_response(url=url, status_code=500)
        chunks = (chunk for chunk in (b"complete ", b"body"))

        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit):
            client.request("PUT", "/pages/raw", data=chunks)

        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert requests[0].content == b"complete body"

    def test_sequence_multipart_stream_disables_automatic_retry(
        self,
        httpx_mock,
        api_key_settings,
    ) -> None:
        url = "https://docs.example.com/api/pages/raw"
        httpx_mock.add_response(url=url, status_code=500)

        class NonSeekableFile:
            def __init__(self) -> None:
                self._stream = io.BytesIO(b"complete body")

            def read(self, size: int = -1) -> bytes:
                return self._stream.read(size)

        files = [("file", ("report.txt", NonSeekableFile(), "text/plain"))]
        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit):
            client.request("PUT", "/pages/raw", files=files)

        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert b"complete body" in requests[0].content

    def test_current_offset_only_multipart_stream_disables_retry(
        self,
        httpx_mock,
        api_key_settings,
    ) -> None:
        url = "https://docs.example.com/api/pages/raw"
        httpx_mock.add_response(url=url, status_code=500)

        class CurrentOffsetOnlyFile:
            def __init__(self) -> None:
                self._stream = io.BytesIO(b"complete body")

            def read(self, size: int = -1) -> bytes:
                return self._stream.read(size)

            def seekable(self) -> bool:
                return False

            def tell(self) -> int:
                return self._stream.tell()

            def seek(self, offset: int, whence: int = 0) -> int:
                if whence != 0 or offset != self._stream.tell():
                    raise OSError("cannot rewind")
                return offset

        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit):
            client.request(
                "PUT",
                "/pages/raw",
                files={"file": ("report.txt", CurrentOffsetOnlyFile(), "text/plain")},
            )

        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert b"complete body" in requests[0].content


class TestAuthenticationReplay:
    def test_one_shot_stream_401_does_not_replay_or_reauthenticate(
        self,
        httpx_mock,
        session_settings,
        monkeypatch,
        tmp_path,
        capsys,
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        url = "https://docs.example.com/api/pages/raw"
        httpx_mock.add_response(url=url, status_code=401)
        chunks = (chunk for chunk in (b"complete ", b"body"))

        with DocmostClient(session_settings) as client, pytest.raises(SystemExit) as exc_info:
            client.request("PUT", "/pages/raw", content=chunks)

        assert exc_info.value.code == 3
        assert len(httpx_mock.get_requests()) == 1
        error = " ".join(capsys.readouterr().err.split())
        assert "cannot be replayed safely" in error
        assert "seekable multipart files" in error

    def test_session_auth_401_replays_post_body_once(
        self,
        httpx_mock,
        session_settings,
        monkeypatch,
        tmp_path,
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        url = "https://docs.example.com/api/users/me"
        httpx_mock.add_response(url=url, status_code=401)
        httpx_mock.add_response(
            url="https://docs.example.com/api/auth/login",
            json={"token": "new_jwt"},
        )
        httpx_mock.add_response(url=url, json={"name": "Retry User"})

        with DocmostClient(session_settings) as client:
            result = client.post("/users/me", json={"include": "profile"})

        requests = [request for request in httpx_mock.get_requests() if str(request.url) == url]
        assert result["name"] == "Retry User"
        assert len(requests) == 2
        assert requests[0].content == requests[1].content == b'{"include":"profile"}'
        assert requests[1].headers["Authorization"] == "Bearer new_jwt"

    def test_session_auth_401_replays_complete_multipart_body(
        self,
        httpx_mock,
        session_settings,
        monkeypatch,
        tmp_path,
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        url = "https://docs.example.com/api/files/upload"
        httpx_mock.add_response(url=url, status_code=401)
        httpx_mock.add_response(
            url="https://docs.example.com/api/auth/login",
            json={"token": "new_jwt"},
        )
        httpx_mock.add_response(url=url, json={"id": "attachment-1"})

        with DocmostClient(session_settings) as client:
            upload_stream = io.BytesIO(b"multipart contents")
            result = client.post_multipart(
                "/files/upload",
                data={"pageId": "page-1"},
                files={"file": ("report.txt", upload_stream, "text/plain")},
            )

        requests = [request for request in httpx_mock.get_requests() if str(request.url) == url]
        assert result == {"id": "attachment-1"}
        assert len(requests) == 2
        for request in requests:
            assert b'name="pageId"' in request.content
            assert b"page-1" in request.content
            assert b'filename="report.txt"' in request.content
            assert b"multipart contents" in request.content
        assert requests[1].headers["Authorization"] == "Bearer new_jwt"

    def test_second_401_is_not_reauthenticated_again(
        self,
        httpx_mock,
        session_settings,
        monkeypatch,
        tmp_path,
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        url = "https://docs.example.com/api/pages/create"
        httpx_mock.add_response(url=url, status_code=401)
        httpx_mock.add_response(
            url="https://docs.example.com/api/auth/login",
            json={"token": "new_jwt"},
        )
        httpx_mock.add_response(url=url, status_code=401)

        with DocmostClient(session_settings) as client, pytest.raises(SystemExit) as exc_info:
            client.post("/pages/create", json={"title": "Only once"})

        assert exc_info.value.code == 3
        assert len(httpx_mock.get_requests()) == 3


class TestTransportFailures:
    def test_get_retries_connect_error_then_succeeds(
        self,
        httpx_mock,
        api_key_settings,
        monkeypatch,
    ) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", sleeps.append)
        url = "https://docs.example.com/api/health"
        httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=url)
        httpx_mock.add_response(url=url, json={"status": "ok"})

        with DocmostClient(api_key_settings) as client:
            result = client.get("/health")

        assert result == {"status": "ok"}
        assert sleeps == [1.0]
        assert len(httpx_mock.get_requests()) == 2

    def test_get_retries_timeout_then_succeeds(
        self,
        httpx_mock,
        api_key_settings,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        url = "https://docs.example.com/api/health"
        httpx_mock.add_exception(httpx.ReadTimeout("read timed out"), url=url)
        httpx_mock.add_response(url=url, json={"status": "ok"})

        with DocmostClient(api_key_settings) as client:
            result = client.get("/health")

        assert result == {"status": "ok"}
        assert len(httpx_mock.get_requests()) == 2

    def test_mutating_post_timeout_is_not_retried_and_warns_of_unknown_outcome(
        self,
        httpx_mock,
        api_key_settings,
        capsys,
    ) -> None:
        url = "https://docs.example.com/api/pages/create"
        httpx_mock.add_exception(httpx.ReadTimeout("read timed out"), url=url)

        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit):
            client.post("/pages/create", json={"title": "Maybe created"})

        assert len(httpx_mock.get_requests()) == 1
        error = " ".join(capsys.readouterr().err.split())
        assert "not retried automatically" in error
        assert "outcome may be unknown" in error
        assert "verify server state" in error

    def test_mutating_post_connect_error_is_not_retried(
        self,
        httpx_mock,
        api_key_settings,
        capsys,
    ) -> None:
        url = "https://docs.example.com/api/pages/create"
        httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=url)

        with DocmostClient(api_key_settings) as client, pytest.raises(SystemExit):
            client.post("/pages/create", json={"title": "Not sent"})

        assert len(httpx_mock.get_requests()) == 1
        error = capsys.readouterr().err
        assert "Cannot connect" in error
        assert "not retried automatically" in error
