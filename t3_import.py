#!/usr/bin/env python3
"""t3-import: bring Claude Code / Codex sessions started in a terminal into T3 Code.

T3 Code can import terminal sessions, but only from its first-run wizard
(pingdotgg/t3code#10961). This calls the same server RPC the wizard uses, so it
can be run at any time, or on a timer.

Runs on the machine that hosts the T3 Code server. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("T3_IMPORT_CONFIG", Path.home() / ".config" / "t3-import"))
TOKEN_FILE = CONFIG_DIR / "token"
RPC_TIMEOUT_SECONDS = 600


class T3Error(Exception):
    pass


# --- locating the server -------------------------------------------------


def t3_home() -> Path:
    return Path(os.environ.get("T3CODE_HOME", Path.home() / ".t3"))


def server_origin(override: str | None) -> str:
    if override:
        return override.rstrip("/")
    runtime = t3_home() / "userdata" / "server-runtime.json"
    try:
        return json.loads(runtime.read_text())["origin"].rstrip("/")
    except (OSError, KeyError, ValueError) as error:
        raise T3Error(
            f"Could not find a running T3 Code server ({runtime}: {error}). "
            "Start it, or pass --origin http://host:port."
        ) from error


def t3_binary() -> str:
    found = shutil.which("t3")
    if found:
        return found
    versions = sorted((t3_home() / "runtime" / "versions").glob("*/t3"))
    if versions:
        return str(versions[-1])
    raise T3Error("Could not find the `t3` binary. Put it on PATH.")


def read_token() -> str:
    token = os.environ.get("T3_IMPORT_TOKEN", "").strip()
    if token:
        return token
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        token = ""
    if not token:
        raise T3Error(
            f"No access token. Run `t3-import login` once, or set T3_IMPORT_TOKEN. (looked in {TOKEN_FILE})"
        )
    return token


# --- HTTP ----------------------------------------------------------------


def http_json(origin: str, token: str, path: str, method: str = "GET"):
    request = urllib.request.Request(
        origin + path,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        data=b"" if method == "POST" else None,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")[:300]
        if error.code in (401, 403):
            raise T3Error(f"T3 Code rejected the access token ({error.code}). Run `t3-import login` again.") from error
        raise T3Error(f"{method} {path} failed: HTTP {error.code} {body}") from error
    except urllib.error.URLError as error:
        raise T3Error(f"Could not reach T3 Code at {origin}: {error.reason}") from error


# --- WebSocket (RFC 6455 client, text frames only) ------------------------


class WebSocket:
    def __init__(self, url: str, timeout: float):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "ws":
            raise T3Error(f"Only ws:// is supported, got {url}")
        port = parts.port or 80
        self.sock = socket.create_connection((parts.hostname, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        self.sock.sendall(
            (
                f"GET {target} HTTP/1.1\r\n"
                f"Host: {parts.hostname}:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        self.buffer = b""
        head = self._read_until(b"\r\n\r\n").decode(errors="replace")
        status = head.split("\r\n", 1)[0]
        if " 101 " not in status:
            raise T3Error(f"WebSocket upgrade refused: {status}")

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise T3Error("T3 Code closed the connection.")
            self.buffer += chunk
        head, self.buffer = self.buffer.split(marker, 1)
        return head

    def _read_exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.sock.recv(max(65536, count - len(self.buffer)))
            if not chunk:
                raise T3Error("T3 Code closed the connection.")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode())

    def receive_text(self) -> str:
        message = b""
        while True:
            first, second = self._read_exact(2)
            opcode, length = first & 0x0F, second & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._read_exact(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._read_exact(8))
            payload = self._read_exact(length)
            if opcode == 0x8:
                raise T3Error("T3 Code closed the connection.")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if first & 0x80:
                return message.decode()

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()


# --- T3 Code RPC -----------------------------------------------------------


def describe_failure(exit_value: dict) -> str:
    reasons = []
    for cause in exit_value.get("cause") or []:
        error = cause.get("error") or cause.get("defect") or cause
        if isinstance(error, dict):
            reasons.append(error.get("message") or error.get("_tag") or json.dumps(error)[:200])
        else:
            reasons.append(str(error)[:200])
    return "; ".join(reasons) or json.dumps(exit_value)[:300]


def rpc(origin: str, token: str, method: str, payload: dict):
    ticket = http_json(origin, token, "/api/auth/websocket-ticket", method="POST")["ticket"]
    ws_url = "ws" + origin[len("http") :] + "/ws?wsTicket=" + urllib.parse.quote(ticket)
    connection = WebSocket(ws_url, timeout=RPC_TIMEOUT_SECONDS)
    try:
        connection.send_text(
            json.dumps({"_tag": "Request", "id": "1", "tag": method, "payload": payload, "headers": []})
        )
        while True:
            decoded = json.loads(connection.receive_text())
            for message in decoded if isinstance(decoded, list) else [decoded]:
                tag = message.get("_tag")
                if tag == "Defect":
                    raise T3Error(f"{method} failed: {json.dumps(message.get('defect'))[:300]}")
                if tag == "Exit" and message.get("requestId") == "1":
                    exit_value = message["exit"]
                    if exit_value.get("_tag") == "Success":
                        return exit_value.get("value")
                    raise T3Error(f"{method} failed: {describe_failure(exit_value)}")
    finally:
        connection.close()


# --- commands ----------------------------------------------------------------


def same_path(left: str, right: str) -> bool:
    return os.path.realpath(left) == os.path.realpath(right)


def projects(origin: str, token: str) -> list[dict]:
    return http_json(origin, token, "/api/orchestration/shell").get("projects", [])


def command_login(args) -> int:
    command = [t3_binary(), "auth", "session", "issue", "--token-only", "--label", "t3-import", "--ttl", args.ttl]
    result = subprocess.run(command, capture_output=True, text=True)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not lines:
        raise T3Error(f"`t3 auth session issue` failed: {result.stderr.strip() or result.stdout.strip()}")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.touch(mode=0o600)
    TOKEN_FILE.chmod(0o600)
    TOKEN_FILE.write_text(lines[-1].strip() + "\n")
    print(f"Saved a T3 Code access token (valid {args.ttl}) to {TOKEN_FILE}")
    print("Revoke it any time with `t3 auth session list` and `t3 auth session revoke`.")
    return 0


def command_scan(args) -> int:
    origin, token = server_origin(args.origin), read_token()
    result = rpc(origin, token, "agentSessions.scan", {})
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    for candidate in result.get("candidates", []):
        mark = "project" if candidate.get("alreadyImported") else "       "
        print(
            f"{mark}  {candidate.get('threadCount', 0):>4} sessions  "
            f"{','.join(candidate.get('sources', [])):<18} {candidate.get('path')}"
        )
    if result.get("truncated"):
        print("Scan limit reached; some directories may be missing.", file=sys.stderr)
    return 0


def command_import(args) -> int:
    origin, token = server_origin(args.origin), read_token()
    known = projects(origin, token)
    if args.paths:
        selected = []
        for path in args.paths:
            matches = [project for project in known if same_path(project["workspaceRoot"], path)]
            if not matches:
                raise T3Error(f"No T3 Code project is rooted at {path}. Add it with `t3 project add`.")
            selected.extend(matches)
    else:
        selected = [project for project in known if not same_path(project["workspaceRoot"], t3_home() / "scratch")]
    failures = 0
    for project in selected:
        try:
            result = rpc(
                origin,
                token,
                "agentSessions.import",
                {"projectId": project["id"], "expectedWorkspaceRoot": project["workspaceRoot"]},
            )
        except T3Error as error:
            failures += 1
            print(f"{project['title']}: {error}", file=sys.stderr)
            continue
        print(
            f"{project['title']}: {result['importedCount']} sessions in T3 Code, "
            f"{result['skippedCount']} skipped  ({project['workspaceRoot']})"
        )
    return 1 if failures else 0


def command_watch(args) -> int:
    while True:
        try:
            command_import(args)
        except T3Error as error:
            print(f"t3-import: {error}", file=sys.stderr)
        sys.stdout.flush()
        time.sleep(args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(prog="t3-import", description=__doc__.split("\n\n")[0])
    parser.add_argument("--origin", help="T3 Code server origin (default: read from ~/.t3)")
    commands = parser.add_subparsers(dest="command", required=True)

    login = commands.add_parser("login", help="issue and store a T3 Code access token for this tool")
    login.add_argument("--ttl", default="90d", help="token lifetime, e.g. 30d (default: 90d)")
    login.set_defaults(run=command_login)

    scan = commands.add_parser("scan", help="list directories that have Claude Code / Codex sessions (read-only)")
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(run=command_scan)

    import_ = commands.add_parser("import", help="import recent sessions into existing T3 Code projects")
    import_.add_argument("paths", nargs="*", help="project directories (default: every project)")
    import_.set_defaults(run=command_import)

    watch = commands.add_parser("watch", help="import repeatedly, for machines without a scheduler")
    watch.add_argument("paths", nargs="*", help="project directories (default: every project)")
    watch.add_argument("--interval", type=int, default=300, help="seconds between imports (default: 300)")
    watch.set_defaults(run=command_watch)

    args = parser.parse_args()
    try:
        return args.run(args)
    except T3Error as error:
        print(f"t3-import: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
