"""Owner login for the household HTTPS listener; private RPC stays on loopback."""

import hashlib
import ipaddress
import secrets
import time
import re
from collections import defaultdict, deque
from pathlib import Path

from pydantic import Field
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse

from .contracts import Contract

# Operational HTTP drain, not part of the pinned business/task profile.
# Matches the workbench's 30s state-request budget, below systemd's 90s stop.
HTTP_SHUTDOWN_SECONDS = 30


class HomeAccessConfig(Contract):
    bind_host: str
    port: int = Field(default=8443, ge=1024, le=65535)
    hosts: list[str] = Field(min_length=1, max_length=8)
    certificate_file: str
    private_key_file: str
    login_code_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    session_seconds: int = Field(default=2592000, ge=300, le=7776000)

    @property
    def origins(self):
        return {"https://" + host.lower() for host in self.hosts}


class HomeAccess:
    def __init__(self, app, config):
        self.app, self.config = app, config
        self.store = app.state.host.store
        self.failed = defaultdict(deque)
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS home_sessions (
                hash TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL)""")

    def session(self, request):
        token = request.cookies.get("muse_session", "")
        if not token or len(token) > 200:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.store.connect() as db:
            row = db.execute("SELECT expires FROM home_sessions WHERE hash=?", (key,)).fetchone()
        return key if row and row[0] > time.time() else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        host = request.headers.get("host", "").lower()
        try:
            address = ipaddress.ip_address(request.client.host)
            local = address.is_private or address.is_loopback
        except ValueError:
            local = False
        if host not in {h.lower() for h in self.config.hosts} or not local:
            return await JSONResponse({"error": "household access only"}, status_code=403)(
                scope, receive, send
            )
        path = scope["path"]
        # Capture endpoints retain their existing source-scoped key validation.
        capture = path.startswith("/harness/capture/")
        if request.method not in {"GET", "HEAD"} and not capture:
            if request.headers.get("origin") not in self.config.origins:
                return await JSONResponse(
                    {"error": "same-origin request required"}, status_code=403
                )(scope, receive, send)
        if path == "/access/login":
            response = await self.login(request)
        elif path == "/access/logout" and request.method == "POST":
            key = self.session(request)
            with self.store.connect() as db:
                db.execute("DELETE FROM home_sessions WHERE hash=?", (key,))
            response = JSONResponse({"ok": True})
            response.delete_cookie("muse_session", secure=True, httponly=True, samesite="strict")
        elif path == "/access/session":
            response = JSONResponse({"authenticated": bool(self.session(request))})
        elif path.startswith("/harness-static/"):
            return await self.app(scope, receive, send)
        elif capture:
            if not re.fullmatch(r"/harness/capture/[\w.:-]+/[\w/-]+", path):
                return await JSONResponse({"error": "invalid capture route"}, status_code=404)(
                    scope, receive, send
                )
            from starlette.responses import StreamingResponse
            from starlette.background import BackgroundTask

            client = self.app.state.host.client
            forwarded = client.build_request(
                request.method,
                path + ("?" + scope["query_string"].decode() if scope["query_string"] else ""),
                headers={
                    key: request.headers.get(key, "")
                    for key in (
                        "authorization",
                        "content-type",
                        "origin",
                        "x-media-event",
                        "x-voice-event",
                    )
                },
                content=request.stream(),
            )
            remote = await client.send(forwarded, stream=True)
            response = StreamingResponse(
                remote.aiter_bytes(),
                status_code=remote.status_code,
                media_type=remote.headers.get("content-type"),
                background=BackgroundTask(remote.aclose),
            )
        elif not self.session(request):
            response = (
                RedirectResponse("/access/login", status_code=303)
                if path in {"/", "/capture"}
                else JSONResponse({"error": "login required"}, status_code=401)
            )
        elif path == "/" or path == "/capture" or path.startswith("/workbench/"):
            return await self.app(scope, receive, send)
        else:
            response = JSONResponse({"error": "private node route"}, status_code=403)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        await response(scope, receive, send)

    async def login(self, request):
        if request.method == "GET":
            return FileResponse(Path(__file__).parent / "web/home-login.html")
        if request.method != "POST":
            return JSONResponse({"error": "method not allowed"}, status_code=405)
        now, peer = time.time(), request.client.host
        attempts = self.failed[peer]
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 5:
            return JSONResponse({"error": "请稍等一分钟再试"}, status_code=429)
        raw = bytearray()
        async for block in request.stream():
            raw.extend(block)
            if len(raw) > 2048:
                return JSONResponse({"error": "request too large"}, status_code=413)
        import json

        try:
            code = json.loads(raw)["code"]
            valid = isinstance(code, str) and secrets.compare_digest(
                hashlib.sha256(code.strip().encode()).hexdigest(), self.config.login_code_sha256
            )
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            attempts.append(now)
            if len(self.failed) > 256:
                self.failed.pop(next(iter(self.failed)))
            return JSONResponse({"error": "配对码不正确"}, status_code=401)
        self.failed.pop(peer, None)
        token = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("DELETE FROM home_sessions WHERE expires<=?", (now,))
            db.execute(
                "INSERT INTO home_sessions VALUES (?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    now,
                    now + self.config.session_seconds,
                ),
            )
            db.execute(
                "DELETE FROM home_sessions WHERE hash IN (SELECT hash FROM home_sessions ORDER BY created DESC LIMIT -1 OFFSET 16)"
            )
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "muse_session",
            token,
            max_age=self.config.session_seconds,
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response


async def serve_home(app, private_port, config, *, log_config=None):
    """Two listeners in one host process and one application lifespan."""
    import asyncio
    import signal
    import logging
    from contextlib import nullcontext
    import uvicorn

    private = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=private_port,
            log_level="warning",
            log_config=log_config,
            access_log=False,
            timeout_graceful_shutdown=HTTP_SHUTDOWN_SECONDS,
        )
    )
    public = uvicorn.Server(
        uvicorn.Config(
            HomeAccess(app, config),
            host=config.bind_host,
            port=config.port,
            ssl_certfile=config.certificate_file,
            ssl_keyfile=config.private_key_file,
            lifespan="off",
            log_level="warning",
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=HTTP_SHUTDOWN_SECONDS,
        )
    )
    private.capture_signals = public.capture_signals = nullcontext
    loop = asyncio.get_running_loop()

    def stop():
        # Uvicorn drains HTTP requests before application lifespan shutdown.
        # Tell previews and producer loops first, so a preview can cancel its
        # FFmpeg child rather than holding the whole process open until SIGKILL.
        app.state.host.stop.set()
        logging.getLogger("uvicorn.error").warning(
            "Muse shutdown requested; draining HTTP tasks private=%s public=%s",
            len(private.server_state.tasks), len(public.server_state.tasks),
        )
        private.should_exit = public.should_exit = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop)
    private_task = asyncio.create_task(private.serve())
    try:
        while not private.started:
            if private_task.done():
                return await private_task
            await asyncio.sleep(0.05)
        public_task = asyncio.create_task(public.serve())
        done, pending = await asyncio.wait(
            {private_task, public_task}, return_when=asyncio.FIRST_COMPLETED
        )
        private.should_exit = public.should_exit = True
        for task in done:
            task.result()
        await asyncio.gather(*pending)
    finally:
        stop()
        await private_task
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
