#!/usr/bin/env python3
"""A mock of the Vexo server-side capture API for SDK conformance tests. Python 3.8+ standard library only.

It accepts and rejects requests the way vexo-api origin/main does (SPEC.md, rules A1-A18), records every request,
and injects faults on demand:

    python3 server.py --port 8765

    POST /_control/faults  {"faults": [{"status": 429, "retryAfter": 2}, {"status": 500, "count": 2},
                                       {"status": 200, "body": "<html>ok</html>"}, {"delayMs": 5000},
                                       {"reset": true}, {"failIndexes": [1]}, {"dropIndexes": [0]}, {}]}
    GET  /_control/state   {"requests": [...], "stored": [...], "pendingFaults": N}
    POST /_control/reset   forget requests, stored messages, faults, and the rate-limit window
"""
import argparse
import json
import math
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Endpoint -> the type it gives its one message (None: /v1/batch carries a batch).
API_PATHS = {"/v1/capture": "capture", "/v1/identify": "identify", "/v1/batch": None}
DEFAULT_API_KEY = "test-key"
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_BATCH_SIZE = 500
MAX_DISTINCT_ID_LENGTH = 200
MAX_EVENT_NAME_LENGTH = 255
MAX_ARGS_LENGTH = 65536
EARLIEST_TIMESTAMP = 946_684_800_000
MAX_FUTURE_SKEW_MS = 3_600_000
RATE_LIMIT_PER_MINUTE = 1200
RETRY_REASON = "Storing failed; retry this message"
TIMESTAMP_REASON = ("timestamp must be epoch milliseconds or an ISO 8601 date-time with an offset, "
                    "no earlier than 2000-01-01T00:00:00Z")
# java.time ISO_OFFSET_DATE_TIME: seconds and fraction optional, case-insensitive, lenient offset.
ISO_OFFSET_DATE_TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{0,9}))?)?"
    r"(Z|[+-][0-9]{2}(?::?[0-9]{2}(?::?[0-9]{2})?)?)", re.IGNORECASE | re.ASCII)


class ApiError(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def envelope(data=None, status=200, message=None):
    return {"success": status < 400, "code": status, "message": message, "data": data}


def utf16_len(text):
    # Java's String.length(), which the API's limits use.
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def iso_millis(text):
    match = ISO_OFFSET_DATE_TIME.fullmatch(text)
    if not match:
        return None
    year, month, day, hour, minute = (int(group) for group in match.groups()[:5])
    second = int(match.group(6) or 0)
    millis = int((match.group(7) or "").ljust(3, "0")[:3])
    offset = match.group(8).upper()
    offset_seconds = 0
    if offset != "Z":
        digits = offset[1:].replace(":", "")
        hours, minutes, seconds = int(digits[0:2]), int(digits[2:4] or 0), int(digits[4:6] or 0)
        if hours > 18 or minutes > 59 or seconds > 59:
            return None
        offset_seconds = (1 if offset[0] == "+" else -1) * (hours * 3600 + minutes * 60 + seconds)
    try:
        local = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:  # Feb 30, hour 24 and the like: java.time parses strictly.
        return None
    return (int(local.timestamp()) - offset_seconds) * 1000 + millis


def epoch_millis(value, now_ms):
    """ServerMessageParser.epochMillis: the timestamp in epoch ms, or None when the API rejects it (A12)."""
    if value is None:
        return now_ms
    millis = None
    if isinstance(value, bool):
        millis = None
    elif isinstance(value, int):
        millis = value if -2 ** 63 <= value < 2 ** 63 else None
    elif isinstance(value, float):
        millis = int(value) if abs(value) < 2.0 ** 63 else None
    elif isinstance(value, str):
        millis = iso_millis(value)
    return None if millis is None or millis < EARLIEST_TIMESTAMP else millis


def parse_message(message, now_ms):
    """(the message as stored, None), or (None, the reason the API drops it) (A10-A13)."""
    if not isinstance(message, dict):
        return None, "message must be a JSON object"
    for field in ("type", "distinctId", "event", "messageId"):
        if message.get(field) is not None and not isinstance(message[field], str):
            return None, field + " must be a string"
    for field in ("properties", "traits"):
        if message.get(field) is not None and not isinstance(message[field], dict):
            return None, field + " must be a JSON object"
    kind = "capture" if message.get("type") is None else message["type"]
    if kind not in ("capture", "identify"):
        return None, "type must be capture or identify"
    # ponytail: str.strip() also trims U+00A0, U+2007 and U+202F, which Java's String.strip() keeps.
    distinct_id = (message.get("distinctId") or "").strip()
    if not distinct_id:
        return None, "distinctId is required"
    if utf16_len(distinct_id) > MAX_DISTINCT_ID_LENGTH:
        return None, "distinctId exceeds %d characters" % MAX_DISTINCT_ID_LENGTH
    message_id = message.get("messageId") or ""
    if kind == "identify":
        return {"type": kind, "distinctId": distinct_id, "traits": message.get("traits"), "messageId": message_id}, None

    event = message.get("event") or ""
    if not event.strip():
        return None, "event is required"
    if utf16_len(event) > MAX_EVENT_NAME_LENGTH:
        return None, "event exceeds %d characters" % MAX_EVENT_NAME_LENGTH
    timestamp = epoch_millis(message.get("timestamp"), now_ms)
    if timestamp is None:
        return None, TIMESTAMP_REASON
    args = dict(message.get("properties") or {})
    args["_routeName"] = "server"
    if utf16_len(json.dumps(args, ensure_ascii=False, separators=(",", ":"))) > MAX_ARGS_LENGTH:
        return None, "Json node cannot exceed %d length" % MAX_ARGS_LENGTH
    return {"type": kind, "distinctId": distinct_id, "event": event, "properties": message.get("properties") or {},
            "timestamp": now_ms if timestamp > now_ms + MAX_FUTURE_SKEW_MS else timestamp,
            "messageId": message_id}, None


def _reject_constant(name):
    raise ValueError("%s isn't JSON" % name)  # Jackson refuses NaN and Infinity; Python's json accepts them.


class MockApi:
    """The API's state: recorded requests, stored messages, pending faults, and the rate-limit window."""

    def __init__(self, api_key=DEFAULT_API_KEY, rate_limit=RATE_LIMIT_PER_MINUTE):
        self.api_key = api_key
        self.rate_limit = rate_limit
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            # A request still being handled from before the reset (a delayed one, say) must not touch the new state.
            self._generation = getattr(self, "_generation", 0) + 1
            self.requests = []
            self.stored = []
            self._faults = []
            self._seen_ids = set()
            self._window = (None, 0)

    def add_faults(self, faults):
        """Queue faults; each applies to the next capture API request, in order. `count` repeats one."""
        with self._lock:
            for fault in faults:
                self._faults.extend([fault] * int(fault.get("count", 1)))

    def state(self):
        with self._lock:
            return {"requests": list(self.requests), "stored": list(self.stored), "pendingFaults": len(self._faults)}

    def handle(self, path, headers, body):
        """Answer one capture API request: (status, JSON body, headers), or None to close without a response."""
        with self._lock:
            fault = self._faults.pop(0) if self._faults else {}
            generation = self._generation
            record = {"time": time.time(), "path": path, "headers": headers, "bytes": len(body),
                      "body": _json_or_none(body), "fault": fault or None,
                      "status": None, "response": None, "respondedAt": None}
            self.requests.append(record)
        if fault.get("delayMs"):
            time.sleep(fault["delayMs"] / 1000)
        if generation != self._generation:
            return None
        result = None
        if fault.get("reset"):
            pass
        elif fault.get("status"):
            status = int(fault["status"])
            retry_after = {"Retry-After": str(fault["retryAfter"])} if "retryAfter" in fault else {}
            result = status, fault.get("body", envelope(status=status, message="Injected fault")), retry_after
        else:
            try:
                result = 200, envelope(self._capture(path, headers, body, fault)), {}
            except ApiError as error:
                retry_after = {"Retry-After": str(error.retry_after)} if error.retry_after else {}
                result = error.status, envelope(status=error.status, message=str(error)), retry_after
        record.update(status=result and result[0], response=result and result[1], respondedAt=time.time())
        return result

    def _capture(self, path, headers, body, fault):
        api_key = headers.get("x-api-key")
        if api_key is None:
            raise ApiError(401, "Missing X-Api-Key token")
        if api_key != self.api_key:
            raise ApiError(401, "Permission denied")
        self._check_rate_limit()
        if len(body) > MAX_BODY_BYTES:
            raise ApiError(413, "The request body exceeds 5 MB")
        try:
            data = json.loads(body, parse_constant=_reject_constant)
        except ValueError:
            raise ApiError(400, "The request body must be valid JSON") from None
        if API_PATHS[path] is None:
            messages = data.get("batch") if isinstance(data, dict) else None
            if not isinstance(messages, list):
                raise ApiError(400, "The body must be a JSON object with a batch array")
        else:
            if isinstance(data, dict):
                data["type"] = API_PATHS[path]
            messages = [data]
        if not messages:
            raise ApiError(400, "Send at least one message")
        if len(messages) > MAX_BATCH_SIZE:
            raise ApiError(400, "A batch holds at most %d messages" % MAX_BATCH_SIZE)

        now_ms = int(time.time() * 1000)
        dropped, failed = [], []
        for index, message in enumerate(messages):
            parsed, reason = parse_message(message, now_ms)
            if reason:
                dropped.append({"index": index, "reason": reason})
            elif index in fault.get("dropIndexes", ()):
                dropped.append({"index": index, "reason": "Injected drop"})
            elif index in fault.get("failIndexes", ()):
                failed.append({"index": index, "reason": RETRY_REASON})
            else:
                self._store(parsed)
        return {"accepted": len(messages) - len(dropped) - len(failed), "dropped": dropped, "failed": failed}

    def _check_rate_limit(self):
        # ponytail: one window for the one API key; per-key windows if the mock ever serves several apps.
        now = time.time()
        minute = int(now // 60)
        with self._lock:
            count = self._window[1] + 1 if self._window[0] == minute else 1
            self._window = (minute, count)
        if count > self.rate_limit:
            raise ApiError(429, "Rate limit exceeded for this app. Maximum %d requests per minute allowed."
                           % self.rate_limit, retry_after=max(1, math.ceil((minute + 1) * 60 - now)))

    def _store(self, message):
        with self._lock:
            message_id = message["messageId"].strip()
            # A18: only captures are deduplicated, and only by a non-blank messageId.
            message["duplicate"] = message["type"] == "capture" and bool(message_id) and message_id in self._seen_ids
            if message["type"] == "capture" and message_id:
                self._seen_ids.add(message_id)
            self.stored.append(message)


def _json_or_none(body):
    try:
        return json.loads(body)
    except ValueError:
        return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VexoMock/1"

    def do_POST(self):
        body = self._read_body()
        path = self.path.split("?")[0]
        api = self.server.api
        if path == "/_control/reset":
            api.reset()
            return self._send(200, {"ok": True})
        if path == "/_control/faults":
            api.add_faults((_json_or_none(body) or {}).get("faults", []))
            return self._send(200, {"ok": True})
        if path not in API_PATHS:
            return self._send(404, envelope(status=404, message="Not found"))
        result = api.handle(path, {name.lower(): value for name, value in self.headers.items()}, body)
        if result is None:
            self.close_connection = True
            return None
        return self._send(*result)

    def do_GET(self):
        if self.path.split("?")[0] == "/_control/state":
            return self._send(200, self.server.api.state())
        return self._send(404, envelope(status=404, message="Not found"))

    def _read_body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline().strip():  # trailers
                        pass
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _send(self, status, payload, headers=None):
        # A string is a raw body from a fault (a proxy's HTML page, say); anything else is JSON.
        raw = isinstance(payload, str)
        data = payload.encode("utf-8") if raw else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html" if raw else "application/json")
        self.send_header("Content-Length", str(len(data)))
        # ponytail: no keep-alive, so the `reset` fault always hits a fresh connection. On a reused one, some HTTP
        # clients (Deno's fetch) resend the request at once by themselves, which hides the SDK's backoff.
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        if self.server.verbose:
            super().log_message(*args)


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, api, verbose=False):
        super().__init__(address, _Handler)
        self.api = api
        self.verbose = verbose

    def handle_error(self, request, client_address):
        # A client that gave up (a timeout, a shutdown deadline) closes the socket under a slow response.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def start(port=0, bind="127.0.0.1", api_key=DEFAULT_API_KEY, verbose=False):
    """Start the mock on a daemon thread and return the server; its port is server.server_address[1]."""
    server = MockServer((bind, port), MockApi(api_key), verbose)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description="Mock of the Vexo server-side capture API.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", default="127.0.0.1", help="use 0.0.0.0 to reach it from a Docker container")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--verbose", action="store_true", help="log each request")
    args = parser.parse_args()
    server = MockServer((args.bind, args.port), MockApi(args.api_key), args.verbose)
    print("Vexo mock API on http://%s:%d (API key %s)" % (args.bind, server.server_address[1], args.api_key),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
