"""Checks that server.py answers like the real API (SPEC.md, A1-A18). Run: python3 -m unittest test_server"""
import json
import unittest
import urllib.error
import urllib.request

import server

KEY = server.DEFAULT_API_KEY


class MockApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.start()
        cls.url = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        self.post("/_control/reset", b"")

    def post(self, path, body, key=KEY):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["X-Api-Key"] = key
        request = urllib.request.Request(self.url + path, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, dict(response.headers), json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, dict(error.headers), json.loads(error.read())

    def state(self):
        with urllib.request.urlopen(self.url + "/_control/state") as response:
            return json.loads(response.read())

    def test_request_errors(self):
        capture = {"distinctId": "u", "event": "e"}
        self.assertEqual(self.post("/v1/batch", {"batch": [capture]}, key=None)[2]["message"], "Missing X-Api-Key token")
        self.assertEqual(self.post("/v1/batch", {"batch": [capture]}, key="nope")[0], 401)
        self.assertEqual(self.post("/v1/batch", b"{nope")[2],
                         {"success": False, "code": 400, "message": "The request body must be valid JSON", "data": None})
        self.assertEqual(self.post("/v1/batch", b"")[0], 400)
        self.assertEqual(self.post("/v1/batch", b'{"batch": [NaN]}')[0], 400)
        self.assertEqual(self.post("/v1/batch", [capture])[2]["message"], "The body must be a JSON object with a batch array")
        self.assertEqual(self.post("/v1/batch", {"batch": []})[2]["message"], "Send at least one message")
        self.assertEqual(self.post("/v1/batch", {"batch": [capture] * 501})[2]["message"], "A batch holds at most 500 messages")
        self.assertEqual(self.post("/v1/batch", {"batch": [{"distinctId": "u", "event": "x" * (5 * 1024 * 1024)}]})[0], 413)

    def test_per_message_drops_and_the_envelope(self):
        long_props = {"p": "x" * (65514 - len('{"p":""}'))}
        batch = [
            {"distinctId": "u", "event": "ok", "timestamp": 1794052800000.9, "messageId": "m1"},  # 0 accepted
            "not an object",                                                                       # 1
            {"distinctId": 7, "event": "e"},                                                       # 2
            {"distinctId": "u", "event": "e", "properties": []},                                   # 3
            {"type": "track", "distinctId": "u", "event": "e"},                                    # 4
            {"distinctId": "  ", "event": "e"},                                                    # 5
            {"distinctId": "d" * 201, "event": "e"},                                               # 6
            {"type": "identify", "distinctId": "u", "traits": {"plan": "pro"}},                    # 7 accepted
            {"distinctId": "u", "event": " "},                                                     # 8
            {"distinctId": "u", "event": "e" * 256},                                               # 9
            {"distinctId": "u", "event": "e", "timestamp": 946684799999},                          # 10
            {"distinctId": "u", "event": "e", "timestamp": "2026-11-07T12:00:00"},                 # 11 no offset
            {"distinctId": "u", "event": "e", "timestamp": "2026-11-07T14:00:00+02:00"},           # 12 accepted
            {"distinctId": "u", "event": "e", "properties": long_props},                           # 13 accepted
            {"distinctId": "u", "event": "e", "properties": dict(long_props, q=1)},                # 14
            {"distinctId": "u", "event": "ok", "messageId": "m1"},                                 # 15 duplicate
        ]
        status, _, body = self.post("/v1/batch", {"batch": batch})
        self.assertEqual(status, 200)
        reasons = {d["index"]: d["reason"] for d in body["data"]["dropped"]}
        self.assertEqual(reasons, {
            1: "message must be a JSON object", 2: "distinctId must be a string",
            3: "properties must be a JSON object", 4: "type must be capture or identify", 5: "distinctId is required",
            6: "distinctId exceeds 200 characters", 8: "event is required", 9: "event exceeds 255 characters",
            10: server.TIMESTAMP_REASON, 11: server.TIMESTAMP_REASON, 14: "Json node cannot exceed 65536 length"})
        self.assertEqual(body["data"]["accepted"], 5)
        stored = self.state()["stored"]
        self.assertEqual(stored[0]["timestamp"], 1794052800000)
        self.assertEqual([m["timestamp"] for m in stored if m["type"] == "capture"][1], 1794052800000)
        self.assertEqual([m["duplicate"] for m in stored], [False, False, False, False, True])

    def test_single_message_endpoints_set_the_type(self):
        self.assertEqual(self.post("/v1/identify", {"type": "capture", "distinctId": "u"})[2]["data"]["accepted"], 1)
        self.assertEqual(self.post("/v1/capture", {"type": "identify", "distinctId": "u"})[2]["data"]["dropped"],
                         [{"index": 0, "reason": "event is required"}])

    def test_faults_apply_in_order_and_requests_are_recorded(self):
        self.post("/_control/faults", {"faults": [{"status": 429, "retryAfter": 3}, {}, {"failIndexes": [1]},
                                                  {"status": 200, "body": "<html>ok</html>"}]})
        batch = {"batch": [{"distinctId": "u", "event": "a"}, {"distinctId": "u", "event": "b"}]}
        status, headers, body = self.post("/v1/batch", batch)
        self.assertEqual((status, headers["Retry-After"], body["success"]), (429, "3", False))
        self.assertEqual(self.post("/v1/batch", batch)[2]["data"]["accepted"], 2)
        self.assertEqual(self.post("/v1/batch", batch)[2]["data"]["failed"],
                         [{"index": 1, "reason": server.RETRY_REASON}])
        with urllib.request.urlopen(urllib.request.Request(self.url + "/v1/batch", data=b"{}", method="POST")) as raw:
            self.assertEqual((raw.status, raw.read()), (200, b"<html>ok</html>"))
        state = self.state()
        self.assertEqual([r["status"] for r in state["requests"]], [429, 200, 200, 200])
        self.assertEqual(state["requests"][0]["headers"]["x-api-key"], KEY)
        self.assertEqual(state["pendingFaults"], 0)

    def test_rate_limit(self):
        self.httpd.api.rate_limit = 2
        try:
            batch = {"batch": [{"distinctId": "u", "event": "a"}]}
            responses = [self.post("/v1/batch", batch) for _ in range(3)]
            self.assertEqual([status for status, _, _ in responses], [200, 200, 429])
            self.assertTrue(1 <= int(responses[2][1]["Retry-After"]) <= 60)
        finally:
            self.httpd.api.rate_limit = server.RATE_LIMIT_PER_MINUTE


if __name__ == "__main__":
    unittest.main()
