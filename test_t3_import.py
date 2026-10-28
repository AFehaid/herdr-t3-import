"""Tests for t3_import against a fake T3 Code server (ticket endpoint, shell snapshot, RPC socket)."""

import base64
import hashlib
import json
import struct
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import t3_import

TOKEN = "test-token"
PROJECT = {"id": "project-1", "title": "demo", "workspaceRoot": "/srv/demo"}


def read_frame(stream) -> str:
    first, second = stream.read(2)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", stream.read(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", stream.read(8))
    mask = stream.read(4)
    return bytes(byte ^ mask[index % 4] for index, byte in enumerate(stream.read(length))).decode()


def frame(text: str, opcode: int = 0x1) -> bytes:
    payload = text.encode()
    if len(payload) < 126:
        return struct.pack("!BB", 0x80 | opcode, len(payload)) + payload
    if len(payload) < 65536:
        return struct.pack("!BBH", 0x80 | opcode, 126, len(payload)) + payload
    return struct.pack("!BBQ", 0x80 | opcode, 127, len(payload)) + payload


class FakeT3(BaseHTTPRequestHandler):
    requests: list = []
    replies: dict = {}

    def log_message(self, *args):
        pass

    def authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self.send_response(401)
        self.end_headers()
        return False

    def send_json(self, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.authorized() and self.path == "/api/auth/websocket-ticket":
            self.send_json({"ticket": "ticket 1", "expiresAt": "2030-01-01T00:00:00Z"})

    def do_GET(self):
        if self.path == "/api/projects":
            if self.authorized():
                self.send_json({"projects": [PROJECT], "threads": []})
            return
        if not self.path.startswith("/ws?"):
            self.send_response(401)
            self.end_headers()
            return
        accept = base64.b64encode(
            hashlib.sha1((self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        self.wfile.write(
            f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )
        request = json.loads(read_frame(self.rfile))
        FakeT3.requests.append(request)
        exit_value = FakeT3.replies[request["tag"]]
        self.wfile.write(frame("", opcode=0x9))
        self.wfile.write(frame(json.dumps({"_tag": "Pong"})))
        self.wfile.write(frame(json.dumps([{"_tag": "Exit", "requestId": request["id"], "exit": exit_value}])))
        self.close_connection = True


class T3ImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeT3)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeT3.requests.clear()

    def test_rpc_returns_the_success_value(self):
        FakeT3.replies = {"agentSessions.scan": {"_tag": "Success", "value": {"candidates": ["x" * 70000]}}}
        result = t3_import.rpc(self.origin, TOKEN, "agentSessions.scan", {})
        self.assertEqual(result, {"candidates": ["x" * 70000]})
        self.assertEqual(FakeT3.requests[0]["payload"], {})

    def test_rpc_reports_the_server_error_message(self):
        failure = {"_tag": "Failure", "cause": [{"_tag": "Fail", "error": {"message": "Project 'p' does not exist."}}]}
        FakeT3.replies = {"agentSessions.import": failure}
        with self.assertRaisesRegex(t3_import.T3Error, "Project 'p' does not exist"):
            t3_import.rpc(self.origin, TOKEN, "agentSessions.import", {"projectId": "p"})

    def test_rejected_token_tells_the_user_to_log_in(self):
        with self.assertRaisesRegex(t3_import.T3Error, "login"):
            t3_import.projects(self.origin, "wrong-token")

    def test_import_sends_the_project_and_its_expected_root(self):
        FakeT3.replies = {"agentSessions.import": {"_tag": "Success", "value": {"importedCount": 3, "skippedCount": 1}}}
        t3_import.os.environ["T3_IMPORT_TOKEN"] = TOKEN
        self.addCleanup(t3_import.os.environ.pop, "T3_IMPORT_TOKEN")
        args = t3_import.argparse.Namespace(origin=self.origin, paths=["/srv/demo"])
        self.assertEqual(t3_import.command_import(args), 0)
        self.assertEqual(
            FakeT3.requests[0]["payload"], {"projectId": "project-1", "expectedWorkspaceRoot": "/srv/demo"}
        )

    def test_import_refuses_a_directory_that_is_not_a_project(self):
        t3_import.os.environ["T3_IMPORT_TOKEN"] = TOKEN
        self.addCleanup(t3_import.os.environ.pop, "T3_IMPORT_TOKEN")
        args = t3_import.argparse.Namespace(origin=self.origin, paths=["/srv/other"])
        with self.assertRaisesRegex(t3_import.T3Error, "No T3 Code project"):
            t3_import.command_import(args)
        self.assertEqual(FakeT3.requests, [])


if __name__ == "__main__":
    unittest.main()
