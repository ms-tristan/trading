"""HTTP transport of the monitoring surface (layer 7, work package wp9).

The transport is deliberately tiny: :class:`MonitoringHandler` turns one HTTP
request into one :meth:`~trading_platform.web.routes.Router.handle` call and
writes the resulting :class:`~trading_platform.web.routes.HttpResponse` back.
Every routing, authentication and payload decision lives in the pure router.

Standard library only (brief D2)
--------------------------------
``http.server.ThreadingHTTPServer`` + ``json`` + ``logging`` + ``threading``.
No WebSocket, no ASGI, no third-party HTTP framework, no CDN and no static
asset: layer 7 serves **JSON only** and the dashboard is a separate application
(``dashboard/``, a Next.js server) that polls this API over plain HTTP.  This is
a deliberate trade-off (documented in ``docs/realtime.md`` as a limitation), and
it is what keeps the whole suite green with the dev extra only.

Two consequences worth stating up front:

* the default ``protocol_version`` (HTTP/1.0) is kept on purpose -- every
  response carries an explicit ``Content-Length`` and the connection is closed
  after it, so a client that sends an unread body can never desynchronise the
  next request on a kept-alive connection;
* ``log_message``/``log_error`` are routed to the ``trading_platform.web``
  logger: the server writes **nothing** to stderr, which is what makes it usable
  as a long-running process.

Threading
---------
Each request is handled in its own daemon thread (``daemon_threads = True``);
the SQLite store of the engine is read through the injected read model, and the
read model keeps its own connection per thread.  The server itself never imports
the orchestrator: it only knows the :class:`SnapshotProvider` protocol, which is
why ``realtime serve`` can expose persisted state with no engine running.
"""

from __future__ import annotations

import json
import logging
import socketserver
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import urlsplit

from trading_platform.config.models import MonitoringConfig
from trading_platform.realtime.monitor import Monitor
from trading_platform.web.routes import (
    LOGGER_NAME,
    HttpResponse,
    Router,
    SnapshotProvider,
    operator_token_from_env,
)

__all__ = [
    "MonitoringHandler",
    "MonitoringServer",
    "SnapshotProvider",
    "create_server",
    "operator_token_from_env",
    "serve",
    "start_in_thread",
]

_LOGGER = logging.getLogger(LOGGER_NAME)

#: Poll interval of the serving loop started by :func:`start_in_thread`.
#: :meth:`~socketserver.BaseServer.shutdown` is only noticed between two polls,
#: so a short interval keeps a start/stop cycle cheap (in a test as in the CLI).
SERVE_POLL_INTERVAL: float = 0.05


def _transport_error(status: int, message: str) -> bytes:
    """Encode a transport-level error body (always valid JSON, never a trace)."""
    return json.dumps({"error": message}).encode("utf-8")


class MonitoringHandler(BaseHTTPRequestHandler):
    """One HTTP request of the monitoring API.

    ``do_GET`` handles both ``GET`` and ``HEAD`` (the router answers ``HEAD``
    exactly like ``GET`` with an empty body), ``do_POST`` handles the single
    mutating route.
    """

    server_version = "trading-platform-monitoring/0.1"

    # -- request entry points ----------------------------------------------

    def do_GET(self) -> None:
        """Answer a GET request through the router."""
        self._answer("GET")

    def do_HEAD(self) -> None:
        """Answer a HEAD request through the router (same status, empty body)."""
        self._answer("HEAD")

    def do_POST(self) -> None:
        """Answer a POST request through the router."""
        self._answer("POST")

    def do_PUT(self) -> None:
        """Answer a PUT request through the router (documented ``405``, as JSON)."""
        self._answer("PUT")

    def do_PATCH(self) -> None:
        """Answer a PATCH request through the router (documented ``405``, as JSON)."""
        self._answer("PATCH")

    def do_DELETE(self) -> None:
        """Answer a DELETE request through the router (documented ``405``, as JSON)."""
        self._answer("DELETE")

    def do_OPTIONS(self) -> None:
        """Answer an OPTIONS request through the router (documented ``405``, as JSON)."""
        self._answer("OPTIONS")

    # -- logging (never stderr) --------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib API
        """Log one request line to ``trading_platform.web`` instead of stderr."""
        _LOGGER.info("%s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib API
        """Log one transport-level error to the logger instead of stderr."""
        _LOGGER.warning("%s - %s", self.address_string(), format % args)

    # -- plumbing -----------------------------------------------------------

    def setup(self) -> None:
        """Bound every socket read by the configured request timeout."""
        super().setup()
        timeout = self._server().router.config.request_timeout_seconds
        self.connection.settimeout(float(timeout))

    def _server(self) -> MonitoringServer:
        """Return this request's server, typed as :class:`MonitoringServer`."""
        return cast("MonitoringServer", self.server)

    def _answer(self, method: str) -> None:
        """Read the request, delegate to the router and write the response."""
        router = self._server().router
        split = urlsplit(self.path)
        body = self._read_body(router.config.max_request_bytes)
        if body is None:  # the transport already answered (400/413)
            return
        response = router.handle(
            method,
            split.path,
            query=split.query,
            body=body,
            headers=self._request_headers(),
        )
        self._write(response, head=method == "HEAD")

    def _request_headers(self) -> dict[str, str]:
        """Return the request headers as a plain, case-preserving mapping."""
        return {str(key): str(value) for key, value in self.headers.items()}

    def _read_body(self, limit: int) -> bytes | None:
        """Read the request body, refusing an oversized one without reading it.

        Returns ``None`` when the request was already answered (a malformed or
        oversized ``Content-Length``); the connection is then closed instead of
        leaving an unread body in the socket.
        """
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError:
            self._refuse(400, f"invalid Content-Length: {raw_length!r}")
            return None
        if length < 0:
            self._refuse(400, f"invalid Content-Length: {raw_length!r}")
            return None
        if length > limit:
            self._refuse(413, f"request body too large: {length} bytes (limit {limit})")
            return None
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _refuse(self, status: int, message: str) -> None:
        """Answer a transport-level refusal and close the connection."""
        body = _transport_error(status, message)
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _write(self, response: HttpResponse, *, head: bool) -> None:
        """Write one router response (never raising on a closed socket)."""
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in response.headers:
                self.send_header(key, value)
            self.end_headers()
            if not head and response.body:
                self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client vanished
            self.close_connection = True


class MonitoringServer(ThreadingHTTPServer):
    """Threading HTTP server exposing one :class:`Router`.

    ``daemon_threads`` keeps a request thread from blocking interpreter exit.
    ``allow_reuse_address`` is ``False`` as frozen by the delivery brief: a
    monitoring port either binds or fails loudly instead of silently sharing a
    socket.  The honest counterpart is that restarting the server on a *fixed*
    port immediately after stopping it can hit a ``TIME_WAIT`` socket; the
    default of ``0`` (ephemeral port) never has that problem, and that is what
    the tests use.

    The bound port is read back from ``server.server_address[1]`` (port ``0``
    therefore works).
    """

    daemon_threads = True
    allow_reuse_address = False

    router: Router

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler] = MonitoringHandler,
        *,
        router: Router,
    ) -> None:
        super().__init__(server_address, handler)
        self.router = router

    def server_bind(self) -> None:
        """Bind the socket **without** the reverse DNS lookup of ``HTTPServer``.

        ``http.server.HTTPServer.server_bind`` resolves the bound address with
        ``socket.getfqdn``, which blocks for seconds (or forever) on a machine
        without working DNS.  A monitoring surface must start offline and must
        start deterministically inside a test, so the server name is the bound
        host itself.
        """
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    @property
    def port(self) -> int:
        """Return the actually bound port (meaningful when ``0`` was requested)."""
        return int(self.server_address[1])


def create_server(
    provider: SnapshotProvider,
    *,
    monitor: Monitor,
    config: MonitoringConfig,
    read_only: bool = True,
    operator_token: str | None = None,
    host: str | None = None,
    port: int | None = None,
    version: str = "",
) -> MonitoringServer:
    """Build (and bind) the monitoring server of a platform.

    ``host``/``port`` default to ``config.host``/``config.port``; ``port=0`` is
    honoured and the ephemeral port is read back from ``server.server_address``.

    When ``operator_token`` is ``None`` the token is resolved from the
    environment through :func:`operator_token_from_env` (``TB_OPERATOR_TOKEN``);
    an empty value means "no token configured", and the mutating route then
    refuses every request -- the fail-safe direction.
    """
    token = operator_token_from_env() if operator_token is None else operator_token
    router = Router(
        provider,
        monitor=monitor,
        config=config,
        read_only=read_only,
        operator_token=token,
        version=version,
    )
    address = (config.host if host is None else host, config.port if port is None else port)
    return MonitoringServer(address, MonitoringHandler, router=router)


def serve(server: MonitoringServer, *, block: bool = True) -> None:
    """Serve until stopped (``block=True``) or exactly one request (``False``)."""
    if block:
        server.serve_forever()
    else:
        server.handle_request()


def start_in_thread(
    server: MonitoringServer, *, poll_interval: float = SERVE_POLL_INTERVAL
) -> threading.Thread:
    """Run ``serve_forever()`` in a daemon thread and return it.

    The socket is already bound and listening when :func:`create_server`
    returns, so a client may connect as soon as this function is called: the
    connection waits in the accept backlog until the serving thread picks it up.

    ``poll_interval`` bounds how long :meth:`~socketserver.BaseServer.shutdown`
    waits before the serving loop notices the stop request; it defaults to a
    short 50 ms so that starting and stopping the monitoring surface inside a
    test costs milliseconds instead of half a second.
    """
    thread = threading.Thread(
        target=partial(server.serve_forever, poll_interval=poll_interval),
        name="trading-platform-monitoring",
        daemon=True,
    )
    thread.start()
    return thread
