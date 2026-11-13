"""The API client (port of src/core.js). One kept-alive connection per thread, no dependencies.
Waits out 429 and server restarts the way the JS bot does."""
from __future__ import annotations

import http.client
import json
import threading
import time
from typing import Any, Optional
from urllib.parse import urlsplit

HOUR, DAY = 60, 1440


class SpreadError(Exception):
    """A request that the server refused. `status` is the HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Api:
    def __init__(self, base: str, key: str):
        self.base, self.key, self.calls = base.rstrip("/"), key, 0
        self._parts = urlsplit(self.base)
        self._local = threading.local()

    def _conn(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            kind = http.client.HTTPSConnection if self._parts.scheme == "https" else http.client.HTTPConnection
            conn = self._local.conn = kind(self._parts.netloc, timeout=20)
        return conn

    def _drop(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
        self._local.conn = None

    def req(self, method: str, path: str, body: Any = None) -> Any:
        payload = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}
        attempt = 0
        while True:
            try:
                conn = self._conn()
                conn.request(method, self._parts.path + path, payload, headers)
                r = conn.getresponse()
                data, status = r.read(), r.status
            except (OSError, http.client.HTTPException):
                self._drop()
                if attempt > 20:
                    raise
                attempt += 1
                time.sleep(1)
                continue
            self.calls += 1
            if status == 429:
                time.sleep(int(r.getheader("Retry-After") or 200) / 1000)
                attempt += 1
                continue
            if 502 <= status <= 504:
                if attempt > 60:
                    raise SpreadError(status, "unreachable")
                attempt += 1
                time.sleep(2)
                continue
            if status == 204:
                return None
            try:
                answer = json.loads(data)
            except ValueError:
                answer = data.decode("utf-8", "replace")
            if not 200 <= status < 300:
                raise SpreadError(status, f"{method} {path} {status} {json.dumps(answer)}")
            return answer

    def create(self, level: int, seed: Optional[str] = None, board: bool = False) -> Any:
        body: dict = {"level": level}
        if seed:
            body["seed"] = seed
        if board:
            body["board"] = True
        return self.req("POST", "/v1/worlds", body)

    def advance(self, world_id: str, body: dict) -> Any:
        return self.req("POST", f"/v1/worlds/{world_id}/advance", body)

    def act(self, world_id: str, actions: list) -> Any:
        return self.req("POST", f"/v1/worlds/{world_id}/actions", actions)

    def snapshot(self, world_id: str) -> Any:
        return self.req("GET", f"/v1/worlds/{world_id}")

    def events(self, world_id: str, after: int = -1) -> Any:
        return self.req("GET", f"/v1/worlds/{world_id}/events?after={after}")

    def score(self, world_id: str) -> Any:
        return self.req("GET", f"/v1/worlds/{world_id}/score")

    def roster(self, world_id: str) -> Any:
        return self.req("GET", f"/v1/worlds/{world_id}/drivers")

    def reveal(self, world_id: str) -> Any:
        return self.req("GET", f"/v1/worlds/{world_id}/reveal")

    def delete(self, world_id: str) -> Any:
        return self.req("DELETE", f"/v1/worlds/{world_id}")

    def map(self) -> Any:
        return self.req("GET", "/v1/map")

    def final(self) -> Any:
        return self.req("GET", "/v1/final")
