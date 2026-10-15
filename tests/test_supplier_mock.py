"""Verify local supplier fixtures, including failure shapes and secret hygiene."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
import io
import json
import socket
import time
import unittest
from contextlib import redirect_stderr

from workbench.testing.supplier_mock import start_mock_server


@contextmanager
def mock(mode="normal", **kwargs):
    defaults = {"reasoning_delay": 0.001, "content_delay": 0.002, "chunk_delay": 0}
    defaults.update(kwargs)
    server, thread = start_mock_server(mode=mode, **defaults)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


def request(server, *, timeout=2, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
    try:
        connection.request("POST", "/v1/chat/completions",
                           json.dumps(body or {"stream": True, "messages": []}),
                           {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        connection.close()


def events(text):
    return [line[6:] if line[6:] == "[DONE]" else json.loads(line[6:])
            for line in text.splitlines() if line.startswith("data: ")]


class SupplierMockTests(unittest.TestCase):
    def test_normal_stream_has_body_reasoning_usage_and_done(self):
        with mock() as server:
            status, headers, text = request(server)
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-Mock-Only"], "true")
            payload = events(text)
            self.assertEqual(payload[-1], "[DONE]")
            deltas = [event["choices"][0]["delta"] for event in payload[:-1] if event["choices"]]
            self.assertEqual("".join(delta.get("content", "") for delta in deltas), "OK")
            self.assertTrue(any(delta.get("reasoning_content") for delta in deltas))
            self.assertEqual(payload[-2]["usage"]["total_tokens"], 6)
            self.assertEqual(server.server_address[0], "127.0.0.1")

    def test_failure_streams_are_distinguishable(self):
        for mode in ("empty", "reasoning-only", "length", "no-done", "truncated"):
            with self.subTest(mode=mode), mock(mode) as server:
                payload = events(request(server)[2])
                chunks = [event for event in payload if isinstance(event, dict) and event["choices"]]
                content = "".join(event["choices"][0]["delta"].get("content", "") for event in chunks)
                reasoning = "".join(event["choices"][0]["delta"].get("reasoning_content", "") for event in chunks)
                finishes = [event["choices"][0]["finish_reason"] for event in chunks]
                if mode in ("empty", "reasoning-only"):
                    self.assertEqual(content, "")
                    self.assertEqual(bool(reasoning), mode == "reasoning-only")
                if mode == "length":
                    self.assertEqual(content, "OK")
                    self.assertIn("length", finishes)
                    self.assertEqual(payload[-1], "[DONE]")
                if mode in ("no-done", "truncated"):
                    self.assertNotIn("[DONE]", payload)
                    self.assertEqual("stop" in finishes, mode == "no-done")

    def test_rate_limit_and_timeout_are_counted_as_arrivals(self):
        with mock("rate-limit") as server:
            status, headers, text = request(server)
            self.assertEqual(status, 429)
            self.assertEqual(headers["Retry-After"], "1")
            self.assertEqual(json.loads(text)["error"]["code"], "rate_limit_exceeded")
            self.assertEqual(server.request_count, 1)
        with mock("timeout", timeout_delay=0.2) as server:
            with self.assertRaises((TimeoutError, socket.timeout)):
                request(server, timeout=0.025)
            self.assertEqual(server.request_count, 1)

    def test_arrivals_concurrent_order_and_snapshot_isolation(self):
        with mock() as server:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: request(server), range(20)))
            self.assertTrue(all(result[0] == 200 for result in results))
            records = server.records_snapshot()
            self.assertEqual([row["sequence"] for row in records], list(range(1, 21)))
            self.assertEqual(server.request_count, 20)
            self.assertTrue(all(row["simulation"] for row in records))
            records[0]["sequence"] = 999
            self.assertEqual(server.records_snapshot()[0]["sequence"], 1)

    def test_no_secrets_body_or_headers_are_echoed_or_recorded(self):
        secret = "sk-a-test-secret-that-must-not-escape"
        stderr = io.StringIO()
        with mock() as server, redirect_stderr(stderr):
            _, _, text = request(server, body={"stream": True, "model": secret,
                "messages": [{"role": "user", "content": secret}],
                "metadata": {"run_id": secret}}, headers={"Authorization": "Bearer " + secret,
                "X-Benchmark-Run-ID": secret})
            saved = json.dumps(server.records_snapshot())
            self.assertNotIn(secret, text + saved + stderr.getvalue())
            self.assertEqual(set(server.records_snapshot()[0]), {
                "sequence", "arrived_monotonic", "arrived_at_unix", "arrived_at_utc", "mode", "simulation"})

    def test_content_delay_is_measured_from_arrival(self):
        with mock(reasoning_delay=0.01, content_delay=0.06) as server:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.request("POST", "/v1/chat/completions", '{"stream":true}',
                                   {"Content-Type": "application/json"})
                response = connection.getresponse()
                observed = {}
                while line := response.readline():
                    if line.startswith(b"data: {"):
                        payload = json.loads(line[6:])
                        for choice in payload["choices"]:
                            for field in ("reasoning_content", "content"):
                                if choice["delta"].get(field) and field not in observed:
                                    observed[field] = time.monotonic()
                arrival = server.records_snapshot()[0]["arrived_monotonic"]
                self.assertGreaterEqual(observed["reasoning_content"] - arrival, 0.008)
                self.assertGreaterEqual(observed["content"] - arrival, 0.055)
                self.assertGreater(observed["content"], observed["reasoning_content"])
            finally:
                connection.close()

    def test_invalid_settings_fail_before_binding(self):
        for kwargs in ({"mode": "other"}, {"port": True}, {"port": -1},
                       {"content_delay": float("nan")}, {"chunk_delay": -1},
                       {"reasoning_delay": 2, "content_delay": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                start_mock_server(**kwargs)


if __name__ == "__main__":
    unittest.main()
