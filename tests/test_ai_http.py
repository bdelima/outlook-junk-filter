"""The AI client against a real (loopback) HTTP server that speaks the OpenAI chat format."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from outlook_junk_filter.ai import AiClient, AiError


@pytest.fixture
def server():
    state = {"requests": [], "status": 200, "content": '{"score": 9, "reason": "pill spam"}'}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, body))
            payload = json.dumps({"choices": [{"message": {"content": state["content"]}}]}).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/v3"
    yield state
    httpd.shutdown()
    httpd.server_close()


def test_a_real_round_trip_returns_the_rating(server):
    result = AiClient(server["url"], "some-model", timeout=5).rate("a@b.example", "Free pills", "click here")
    assert (result.score, result.reason) == (9.0, "pill spam")
    path, body = server["requests"][0]
    assert path == "/v3/chat/completions" and body["model"] == "some-model" and body["max_tokens"] == 80
    assert "Free pills" in body["messages"][1]["content"] and "click here" in body["messages"][1]["content"]


def test_a_real_http_error_is_a_server_down_error(server):
    server["status"] = 500
    with pytest.raises(AiError) as info:
        AiClient(server["url"], "m", timeout=5).rate("a", "s", "t")
    assert info.value.server_down is True


def test_a_server_that_is_not_listening_is_a_server_down_error():
    with pytest.raises(AiError) as info:
        AiClient("http://127.0.0.1:9/v3", "m", timeout=2).rate("a", "s", "t")
    assert info.value.server_down is True
