"""A tiny in-memory GitHub REST stub: git trees, contents and commits, with ETag/304 support.

It answers exactly the endpoints `uam.clients.github.HttpGitHubClient` calls. Every repo is a
linear history of commits; each commit holds a full snapshot of the repo's files. A `ref` may be a
branch name (`main`, the head) or the sha of any commit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


@dataclass
class _Commit:
    sha: str
    files: dict[str, str]
    committed_at: datetime


@dataclass
class _Repo:
    commits: list[_Commit] = field(default_factory=list)


def _blob_sha(text: str) -> str:
    return hashlib.sha1(f"blob {len(text)}\0{text}".encode(), usedforsecurity=False).hexdigest()  # noqa: S324


class StubGitHub:
    """State plus the FastAPI app; `start()` serves it on a free local port in a thread."""

    def __init__(self) -> None:
        self._repos: dict[str, _Repo] = {}
        self._counter = 0
        self._lock = threading.Lock()
        self.request_log: list[tuple[str, str, int]] = []
        self.app = self._build_app()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # ------------------------------------------------------------------ state

    def commit(self, repo: str, files: dict[str, str | None]) -> str:
        """Add a commit to `owner/repo` that changes `files` (None deletes). Returns the commit sha."""
        with self._lock:
            r = self._repos.setdefault(repo, _Repo())
            snapshot = dict(r.commits[-1].files) if r.commits else {}
            for path, text in files.items():
                if text is None:
                    snapshot.pop(path, None)
                else:
                    snapshot[path] = text
            self._counter += 1
            sha = hashlib.sha1(f"{repo}:{self._counter}".encode(), usedforsecurity=False).hexdigest()  # noqa: S324
            when = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=self._counter)
            r.commits.append(_Commit(sha, snapshot, when))
            return sha

    def head(self, repo: str) -> str:
        """Sha of the newest commit of `owner/repo`."""
        return self._repos[repo].commits[-1].sha

    def _resolve(self, repo: str, ref: str) -> int | None:
        r = self._repos.get(repo)
        if r is None or not r.commits:
            return None
        for i, c in enumerate(r.commits):
            if c.sha == ref:
                return i
        if ref in ("main", "master", "HEAD"):
            return len(r.commits) - 1
        return None

    # ------------------------------------------------------------------ http

    @property
    def url(self) -> str:
        """Base URL to pass as `AppConfig.github_api_url`."""
        return f"http://127.0.0.1:{self.port}"

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None)

        def not_found() -> Response:
            return JSONResponse({"message": "Not Found"}, status_code=404)

        def etagged(request: Request, payload: object) -> Response:
            body = json.dumps(payload)
            etag = '"' + hashlib.sha1(body.encode(), usedforsecurity=False).hexdigest() + '"'  # noqa: S324
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers={"ETag": etag})
            return Response(body, media_type="application/json", headers={"ETag": etag})

        @app.middleware("http")
        async def log_requests(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            resp = await call_next(request)
            self.request_log.append((request.method, str(request.url.path), resp.status_code))
            return resp

        @app.get("/repos/{owner}/{repo}/git/trees/{ref}")
        async def tree(owner: str, repo: str, ref: str, request: Request) -> Response:
            idx = self._resolve(f"{owner}/{repo}", ref)
            if idx is None:
                return not_found()
            files = self._repos[f"{owner}/{repo}"].commits[idx].files
            entries = [
                {"path": p, "type": "blob", "mode": "100644", "sha": _blob_sha(t)}
                for p, t in sorted(files.items())
            ]
            return etagged(request, {"sha": "tree", "tree": entries, "truncated": False})

        @app.get("/repos/{owner}/{repo}/contents/{path:path}")
        async def contents(owner: str, repo: str, path: str, request: Request, ref: str = "main") -> Response:
            idx = self._resolve(f"{owner}/{repo}", ref)
            if idx is None:
                return not_found()
            text = self._repos[f"{owner}/{repo}"].commits[idx].files.get(path)
            if text is None:
                return not_found()
            return etagged(
                request,
                {
                    "type": "file",
                    "path": path,
                    "sha": _blob_sha(text),
                    "encoding": "base64",
                    "content": base64.encodebytes(text.encode()).decode(),
                },
            )

        @app.get("/repos/{owner}/{repo}/commits")
        async def commits(
            owner: str, repo: str, request: Request, path: str = "", sha: str = "main", per_page: int = 30
        ) -> Response:
            key = f"{owner}/{repo}"
            idx = self._resolve(key, sha)
            if idx is None:
                return not_found()
            history = self._repos[key].commits
            out: list[dict[str, object]] = []
            for i in range(idx, -1, -1):
                here = history[i].files.get(path)
                before = history[i - 1].files.get(path) if i > 0 else None
                if here != before:
                    stamp = history[i].committed_at.isoformat().replace("+00:00", "Z")
                    out.append({"sha": history[i].sha, "commit": {"committer": {"date": stamp}}})
                    if len(out) >= per_page:
                        break
            return etagged(request, out)

        return app

    def start(self) -> None:
        """Serve the stub on a free port in a daemon thread and wait until it answers."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("GitHub stub did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        """Stop the server thread."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
