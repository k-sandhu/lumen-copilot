"""Deadline-aware Docker transport, scoped to the execution thread's context."""

from __future__ import annotations

import contextlib
import socket

from docker import APIClient, DockerClient

from lumen_sandbox_runner.execution import check_deadline, current_deadline


class DeadlineAPIClient(APIClient):
    def _set_request_timeout(self, kwargs):
        kwargs = super()._set_request_timeout(kwargs)
        remaining = check_deadline()
        if remaining is not None:
            kwargs["timeout"] = remaining
        return kwargs

    def _disable_socket_timeout(self, sock):
        remaining = check_deadline()
        if remaining is None:
            return super()._disable_socket_timeout(sock)
        for candidate in (sock, getattr(sock, "_sock", None)):
            if hasattr(candidate, "settimeout"):
                candidate.settimeout(remaining)

    def _stream_raw_result(self, response, chunk_size=1, decode=True):
        # The SDK disables read timeouts for archives. Retain the socket deadline
        # and actively interrupt it on expiry, including a peer trickling bytes.
        deadline = current_deadline.get()
        if deadline is None:
            yield from super()._stream_raw_result(response, chunk_size, decode)
            return
        sock = self._get_raw_response_socket(response)

        def interrupt():
            raw = getattr(sock, "_sock", sock)
            with contextlib.suppress(OSError):
                raw.shutdown(socket.SHUT_RDWR)

        try:
            with deadline.watch(interrupt):
                self._disable_socket_timeout(sock)
                for chunk in response.iter_content(chunk_size, decode):
                    deadline.remaining()
                    yield chunk
        finally:
            response.close()


class DeadlineDockerClient(DockerClient):
    def __init__(self, *args, **kwargs):
        self.api = DeadlineAPIClient(*args, **kwargs)
