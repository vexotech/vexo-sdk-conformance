#!/usr/bin/env python3
"""Run the conformance scenarios against an SDK driver. Python 3.8+ standard library only.

    python3 run.py --driver "node ../vexo-node/conformance/driver.js"
    python3 run.py --driver "python3 ../vexo-python/conformance/driver.py" --only basic,retry_after
    python3 run.py --list

Each scenario starts from a clean mock API (server.py), pipes the scenario to the driver on stdin, waits for the
driver to exit, and checks what the mock recorded. README.md describes the driver protocol; SPEC.md the rules.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # so `python3 -I run.py` finds server.py
import server  # noqa: E402

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
USER_AGENT = re.compile(r"vexo-[a-z0-9]+/[0-9A-Za-z.+-]+")
# Seconds of scheduling and startup slack on timing checks.
SLACK = 0.5

SCENARIOS = []


def scenario(refs, steps, options=None, faults=None, api_key=server.DEFAULT_API_KEY):
    """Register a check function as a scenario: the driver runs `steps`, then the check inspects the run."""
    def register(check):
        SCENARIOS.append({"name": check.__name__, "refs": refs, "steps": steps, "options": options or {},
                          "faults": faults or [], "api_key": api_key, "check": check, "doc": check.__doc__})
        return check
    return register


def capture(i, **fields):
    return dict({"op": "capture", "distinctId": "user-1", "event": "event_%d" % i, "properties": {"i": i}}, **fields)


class Run:
    """What one scenario produced: the mock's records and the driver's output."""

    def __init__(self, state, lines, seconds, returncode, output):
        self.requests = state["requests"]
        self.stored = state["stored"]
        self.errors = [line for line in lines if line.get("type") == "error"]
        self.steps = [line for line in lines if line.get("type") == "step"]
        self.seconds = seconds
        self.returncode = returncode
        self.output = output

    def batch(self, i):
        return self.requests[i]["body"]["batch"]

    def sent(self):
        return [message for request in self.requests for message in (request["body"] or {}).get("batch", [])]

    def events(self, i):
        return [message.get("event") for message in self.batch(i)]

    def stored_events(self):
        return [message["event"] for message in self.stored if message["type"] == "capture" and not message["duplicate"]]

    def gap(self, i):
        """Seconds between the response to request i-1 and request i."""
        return self.requests[i]["time"] - self.requests[i - 1]["respondedAt"]

    def step_ms(self, op):
        return next(step["ms"] for step in self.steps if step.get("op") == op)


def expect(condition, message, *args):
    if not condition:
        raise AssertionError(message % args if args else message)


def expect_requests(run, count):
    expect(len(run.requests) == count, "expected %d request(s), got %d", count, len(run.requests))


def expect_no_errors(run):
    expect(not run.errors, "expected no reported errors, got %s", [error["message"] for error in run.errors])


# --- Scenarios -------------------------------------------------------------------------------------------------

NOW_ISO, NOW_MS = "2026-11-07T12:00:00Z", 1794052800000


@scenario(["W1", "W2", "M1", "M2", "M3", "M5", "C16"], [
    {"op": "identify", "distinctId": "user-1", "traits": {"plan": "pro"}},
    {"op": "capture", "distinctId": "user-1", "event": "purchase_completed",
     "properties": {"amount": 49.9, "currency": "USD"}},
    {"op": "capture", "distinctId": "user-1", "event": "native_date", "at": NOW_ISO},
    {"op": "capture", "distinctId": "user-1", "event": "epoch_ms", "timestamp": NOW_MS + 123},
    {"op": "capture", "distinctId": "user-1", "event": "own_id", "messageId": "order-1234-paid"},
    {"op": "flush"},
    {"op": "shutdown"},
])
def basic(run):
    """An identify and four captures go out in one batch with the right fields, timestamps, and message IDs."""
    expect_requests(run, 1)
    identify, purchase, native, epoch, own = run.batch(0)
    expect(identify["type"] == "identify" and identify["distinctId"] == "user-1", "first message: %s", identify)
    expect(identify["traits"] == {"plan": "pro"}, "traits: %s", identify.get("traits"))
    expect("timestamp" not in identify, "an identify has no timestamp (M2): %s", identify)
    expect(purchase["type"] == "capture" and purchase["event"] == "purchase_completed", "second message: %s", purchase)
    expect(purchase["properties"] == {"amount": 49.9, "currency": "USD"}, "properties: %s", purchase.get("properties"))
    expect(native["properties"] == {}, "properties default to {} (M1): %s", native.get("properties"))
    now_ms = time.time() * 1000
    expect(isinstance(purchase["timestamp"], int) and abs(purchase["timestamp"] - now_ms) < 60000,
           "a default timestamp is integer epoch ms near now (M5): %r", purchase["timestamp"])
    expect(native["timestamp"] == NOW_MS, "a native date-time %s becomes %d (M5), got %r",
           NOW_ISO, NOW_MS, native["timestamp"])
    expect(epoch["timestamp"] == NOW_MS + 123, "epoch ms is sent unchanged, got %r", epoch["timestamp"])
    expect(own["messageId"] == "order-1234-paid", "a caller's messageId is sent unchanged (M3), got %r",
           own["messageId"])
    ids = [message["messageId"] for message in (identify, purchase, native, epoch)]
    expect(len(set(ids)) == 4, "message IDs are unique: %s", ids)
    expect(run.requests[0]["response"]["data"] == {"accepted": 5, "dropped": [], "failed": []},
           "the API accepts all five: %s", run.requests[0]["response"])
    expect_no_errors(run)


@scenario(["C3", "C6"], [capture(i) for i in range(7)] + [{"op": "sleep", "ms": 1500}, {"op": "exit"}],
          options={"flushAt": 3, "flushIntervalMs": 60000})
def batch_size(run):
    """With flushAt 3 and no flush call, the first six messages go out on their own, in order, 3 at most per batch."""
    expect(len(run.requests) >= 2, "expected the queue size to trigger at least 2 batches, got %d", len(run.requests))
    expect(all(len(run.batch(i)) <= 3 for i in range(len(run.requests))), "a batch holds at most flushAt (3)")
    events = [message["event"] for message in run.sent()]
    expect(events[:6] == ["event_%d" % i for i in range(6)], "sent in queue order: %s", events)


@scenario(["C4"], [capture(0), capture(1), {"op": "sleep", "ms": 2000}, {"op": "exit"}],
          options={"flushAt": 100, "flushIntervalMs": 500})
def batch_interval(run):
    """With flushIntervalMs 500 and no flush call, queued messages go out on the timer."""
    expect([message["event"] for message in run.sent()] == ["event_0", "event_1"],
           "expected both messages sent by the interval, got %s", [message["event"] for message in run.sent()])


@scenario(["C4", "C22"], [capture(0), capture(1), {"op": "sleep", "ms": 1500}, {"op": "exit"}],
          options={"flushAt": 100, "flushIntervalMs": 0})
def interval_off(run):
    """With flushIntervalMs 0 (the timer off), nothing goes out without flushAt, flush, or shutdown."""
    expect_requests(run, 0)


@scenario(["M4", "C7"], [capture(0), capture(1), capture(2), {"op": "flush"}, {"op": "shutdown"}],
          faults=[{"status": 500}])
def retry_same_message_id(run):
    """A batch that gets a 500 is resent with the same message IDs and bodies."""
    expect_requests(run, 2)
    expect(run.batch(0) == run.batch(1), "the retry differs from the first attempt:\n%s\n%s", run.batch(0), run.batch(1))
    expect(run.stored_events() == ["event_0", "event_1", "event_2"], "stored: %s", run.stored_events())
    expect_no_errors(run)


@scenario(["C9"], [capture(0), {"op": "flush"}, {"op": "shutdown"}], faults=[{"status": 429, "retryAfter": 2}])
def retry_after(run):
    """A 429 with Retry-After: 2 is retried after 2 seconds, not after the shorter backoff."""
    expect_requests(run, 2)
    expect(run.gap(1) >= 2 - 0.05, "waited %.2fs; Retry-After asked for 2s", run.gap(1))
    expect(run.gap(1) <= 2 + SLACK * 4, "waited %.2fs; Retry-After asked for 2s", run.gap(1))
    expect(run.stored_events() == ["event_0"], "stored: %s", run.stored_events())
    expect_no_errors(run)


@scenario(["C7", "C8"], [capture(0), {"op": "flush"}, {"op": "shutdown"}],
          options={"maxRetries": 3}, faults=[{"status": 503}, {"reset": True}, {"status": 502}])
def backoff_5xx(run):
    """503, a dropped connection, and 502 are retried with growing jittered backoff: [0.5,1]s, [1,2]s, [2,4]s."""
    expect_requests(run, 4)
    for i, low in ((1, 0.5), (2, 1.0), (3, 2.0)):
        expect(low - 0.05 <= run.gap(i) <= 2 * low + SLACK * 2, "retry %d waited %.2fs, expected %.1f-%.1fs",
               i, run.gap(i), low, 2 * low)
    expect(run.stored_events() == ["event_0"], "stored: %s", run.stored_events())
    expect_no_errors(run)


@scenario(["C10"], [capture(0), {"op": "flush"}, capture(1), {"op": "flush"}, {"op": "shutdown"}],
          faults=[{"status": 400}])
def no_retry_on_4xx(run):
    """A 400 isn't retried: it's reported with its status, and the next batch goes out normally."""
    expect_requests(run, 2)
    expect(run.events(0) == ["event_0"] and run.events(1) == ["event_1"], "batches: %s, %s", run.events(0), run.events(1))
    expect(any(error.get("status") == 400 for error in run.errors), "expected an error with status 400, got %s",
           run.errors)


@scenario(["A1", "C10"], [capture(0), {"op": "flush"}, {"op": "shutdown"}], api_key="not-the-key")
def bad_api_key(run):
    """A key the API doesn't know gets a 401, which is reported and not retried."""
    expect_requests(run, 1)
    expect(run.requests[0]["status"] == 401, "the mock answered %s", run.requests[0]["status"])
    expect(any(error.get("status") == 401 for error in run.errors), "expected an error with status 401, got %s",
           run.errors)


@scenario(["M4", "A15", "C14"], [capture(0), capture(1), capture(2), {"op": "flush"}, {"op": "shutdown"}],
          faults=[{"failIndexes": [1]}])
def failed_resend(run):
    """A message the API lists in `failed` is resent alone, with the same message ID; the others aren't."""
    expect_requests(run, 2)
    expect(run.batch(1) == [run.batch(0)[1]], "expected only the failed message resent, got %s", run.batch(1))
    expect(sorted(run.stored_events()) == ["event_0", "event_1", "event_2"], "stored: %s", run.stored_events())
    expect_no_errors(run)


@scenario(["M8", "M10", "C15"], [
    capture(0, event="valid_a"),
    capture(1, event="poison", poison=True),
    capture(2, event="not_a_number", nan=True),
    capture(3, event="x" * 256),
    capture(4, event="valid_b"),
    {"op": "flush"},
    {"op": "shutdown"},
])
def poison_isolated(run):
    """Unserializable and NaN messages are reported at enqueue, one the API drops is reported, nothing is resent."""
    expect_requests(run, 1)
    events = run.events(0)
    expect("poison" not in events, "the unserializable message was sent: %s", events)
    expect("not_a_number" not in events, "the NaN message was sent (M8: drop it, never send null or NaN): %s", events)
    expect(events[0] == "valid_a" and events[-1] == "valid_b", "the valid messages weren't sent: %s", events)
    expect(run.stored_events() == ["valid_a", "valid_b"], "stored: %s", run.stored_events())
    expect(len(run.errors) >= 3, "expected the poison, NaN, and dropped messages reported, got %s",
           [error["message"] for error in run.errors])


@scenario(["W5", "C6", "M9"],
          [capture(i, properties={"blob": "x" * 60000}) for i in range(100)]
          + [capture(100, event="too_big", properties={"blob": "x" * (5 * 1024 * 1024)}),
             {"op": "flush"}, {"op": "shutdown"}],
          options={"flushAt": 500})
def split_5mb(run):
    """100 messages of 60 KB (6 MB) split into requests under 5 MB; a single message over 5 MB is reported."""
    expect(len(run.requests) >= 2, "expected at least 2 requests, got %d", len(run.requests))
    expect(all(request["status"] == 200 for request in run.requests), "statuses: %s",
           [request["status"] for request in run.requests])
    expect(sorted(run.stored_events()) == sorted("event_%d" % i for i in range(100)), "stored %d of 100 messages",
           len(run.stored_events()))
    expect("too_big" not in [message["event"] for message in run.sent()], "the message over 5 MB was sent")
    expect(len(run.errors) >= 1, "expected the message over 5 MB reported")


@scenario(["C17"], [capture(0), capture(1), {"op": "shutdown"}],
          options={"shutdownTimeoutMs": 1000, "timeoutMs": 30000}, faults=[{"delayMs": 8000, "count": 10}])
def shutdown_deadline(run):
    """With a server that answers after 8 s, shutdown gives up at its 1 s deadline and reports what it couldn't send."""
    expect(run.step_ms("shutdown") <= 1000 + SLACK * 1000 * 3, "shutdown took %d ms; the deadline is 1000 ms",
           run.step_ms("shutdown"))
    expect(run.seconds < 7, "the driver took %.1fs to exit; exit must not wait for the 8 s response", run.seconds)
    expect(len(run.errors) >= 1, "expected the undelivered messages reported")


@scenario(["C17"], [capture(0), {"op": "sleep", "ms": 300}, {"op": "shutdown"}],
          options={"flushAt": 1, "shutdownTimeoutMs": 1000, "timeoutMs": 30000}, faults=[{"delayMs": 8000}])
def shutdown_inflight(run):
    """A request the SDK started on its own is still in flight at shutdown: the deadline aborts it and reports it."""
    expect_requests(run, 1)
    expect(run.step_ms("shutdown") <= 1000 + SLACK * 1000 * 3, "shutdown took %d ms; the deadline is 1000 ms",
           run.step_ms("shutdown"))
    expect(run.seconds < 7, "the driver took %.1fs to exit; exit must not wait for the 8 s response", run.seconds)
    expect(len(run.errors) >= 1, "expected the aborted batch reported")


@scenario(["C17"], [capture(0), {"op": "sleep", "ms": 500}, {"op": "shutdown"}],
          options={"flushAt": 1, "shutdownTimeoutMs": 1000}, faults=[{"status": 429, "retryAfter": 30}])
def shutdown_backoff(run):
    """The SDK is waiting out Retry-After: 30 at shutdown: the deadline ends the wait, and no retry starts after it."""
    expect_requests(run, 1)
    expect(run.step_ms("shutdown") <= 1000 + SLACK * 1000 * 3, "shutdown took %d ms; the deadline is 1000 ms",
           run.step_ms("shutdown"))
    expect(len(run.errors) >= 1, "expected the unsent batch reported")


@scenario(["W6"], [capture(0), {"op": "flush"}, {"op": "shutdown"}],
          faults=[{"status": 200, "body": "<html>ok</html>"}])
def unparseable_2xx(run):
    """A 200 whose body isn't the JSON envelope (a proxy page, say) still means accepted: no retry, no error."""
    expect_requests(run, 1)
    expect_no_errors(run)


@scenario(["C13"], [capture(0), {"op": "flush"}, capture(1), {"op": "flush"}, capture(2), {"op": "flush"},
                    capture(3), {"op": "flush"}, {"op": "shutdown"}],
          options={"maxRetries": 2}, faults=[{"status": 503, "count": 4}, {}, {"status": 503}])
def circuit_breaker(run):
    """After a batch uses up its retries, the next gets one attempt; a success restores the retries."""
    attempts = [run.events(i)[0] for i in range(len(run.requests))]
    expect(attempts == ["event_0"] * 3 + ["event_1", "event_2", "event_3", "event_3"],
           "expected attempts 3, 1, 1, 2 for the four batches, got %s", attempts)
    expect(run.stored_events() == ["event_2", "event_3"], "stored: %s", run.stored_events())
    expect(len(run.errors) >= 2, "expected the first two batches reported")


@scenario(["C5"], [capture(0), capture(1), capture(2), {"op": "flush"}, {"op": "shutdown"}],
          options={"maxQueueSize": 2})
def queue_full(run):
    """With maxQueueSize 2, the third message is reported and dropped; the first two are sent."""
    expect([message["event"] for message in run.sent()] == ["event_0", "event_1"], "sent: %s",
           [message["event"] for message in run.sent()])
    expect(len(run.errors) >= 1, "expected the dropped message reported")


@scenario(["C19"], [capture(0)])
def exit_hook(run):
    """A program that queues a message and ends without flush or shutdown still sends it."""
    expect(run.stored_events() == ["event_0"], "stored: %s", run.stored_events())
    expect_no_errors(run)


# --- Runner ----------------------------------------------------------------------------------------------------

def check_wire(run, api_key):
    """W1, W2, W4, M1-M3, M5 on every request of every scenario."""
    for i, request in enumerate(run.requests):
        headers = request["headers"]
        expect(request["path"] == "/v1/batch", "request %d went to %s (W1)", i, request["path"])
        expect(headers.get("x-api-key") == api_key, "request %d X-Api-Key: %r (W2)", i, headers.get("x-api-key"))
        expect(headers.get("content-type", "").startswith("application/json"), "request %d Content-Type: %r (W2)",
               i, headers.get("content-type"))
        expect(USER_AGENT.fullmatch(headers.get("user-agent", "")), "request %d User-Agent: %r (W2)",
               i, headers.get("user-agent"))
        batch = (request["body"] or {}).get("batch")
        expect(isinstance(batch, list) and 1 <= len(batch) <= 500, "request %d body isn't a batch of 1-500 (W1, W4)", i)
        for message in batch:
            expect(message.get("type") in ("capture", "identify"), "message type: %r (M1, M2)", message.get("type"))
            message_id = message.get("messageId")
            expect(message_id == "order-1234-paid" or UUID.fullmatch(str(message_id)), "messageId: %r (M3)", message_id)
            if message["type"] == "capture":
                expect(isinstance(message.get("properties"), dict), "properties: %r (M1)", message.get("properties"))
                # M5: integer epoch ms, or a string the caller passed (`basic` checks the ones the SDK produces).
                timestamp = message.get("timestamp")
                expect(type(timestamp) is int or isinstance(timestamp, str), "timestamp: %r (M5)", timestamp)
            else:
                expect(isinstance(message.get("traits"), dict), "traits: %r (M2)", message.get("traits"))


def run_scenario(httpd, spec, driver, host, timeout):
    httpd.api.reset()
    httpd.api.add_faults(spec["faults"])
    payload = {"host": host, "apiKey": spec["api_key"], "options": spec["options"], "steps": spec["steps"]}
    start = time.monotonic()
    proc = subprocess.run(shlex.split(driver), input=json.dumps(payload).encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    seconds = time.monotonic() - start
    time.sleep(0.2)  # let a response written as the driver exited land in the records
    output = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    lines = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            lines.append(parsed)
    return Run(httpd.api.state(), lines, seconds, proc.returncode, output)


def main():
    parser = argparse.ArgumentParser(description="Run the Vexo SDK conformance scenarios against a driver.")
    parser.add_argument("--driver", help='the command that runs the driver, for example "node driver.js"')
    parser.add_argument("--only", help="comma-separated scenarios to run")
    parser.add_argument("--skip", help="comma-separated scenarios to skip (say why in your report)")
    parser.add_argument("--port", type=int, default=0, help="mock API port (default: a free one)")
    parser.add_argument("--bind", default="127.0.0.1", help="use 0.0.0.0 for a driver in a Docker container")
    parser.add_argument("--host-url", default="http://127.0.0.1:{port}",
                        help="the host the driver gets; {port} is replaced (Docker: http://host.docker.internal:{port})")
    parser.add_argument("--timeout", type=float, default=60, help="seconds before a driver run is killed")
    parser.add_argument("--list", action="store_true", help="list the scenarios and exit")
    parser.add_argument("--verbose", action="store_true", help="print the driver output and requests on failure")
    args = parser.parse_args()

    if args.list:
        for spec in SCENARIOS:
            print("%-22s %-28s %s" % (spec["name"], ",".join(spec["refs"]), spec["doc"]))
        return 0
    if not args.driver:
        parser.error("--driver is required")
    only = set(args.only.split(",")) if args.only else None
    skip = set(args.skip.split(",")) if args.skip else set()
    unknown = ((only or set()) | skip) - {spec["name"] for spec in SCENARIOS}
    if unknown:
        parser.error("unknown scenario(s): %s" % ", ".join(sorted(unknown)))

    httpd = server.start(args.port, args.bind)
    host = args.host_url.format(port=httpd.server_address[1])
    passed = failed = skipped = 0
    for spec in SCENARIOS:
        name = spec["name"]
        if (only and name not in only) or name in skip:
            skipped += 1
            continue
        run = None
        try:
            run = run_scenario(httpd, spec, args.driver, host, args.timeout)
            check_wire(run, spec["api_key"])
            spec["check"](run)
        except subprocess.TimeoutExpired:
            failed += 1
            print("FAIL %-22s the driver didn't exit within %.0fs" % (name, args.timeout), flush=True)
        except Exception as error:  # noqa: BLE001 -- a broken check is a failed scenario, not a crashed run
            failed += 1
            print("FAIL %-22s %s: %s" % (name, type(error).__name__, error), flush=True)
            if args.verbose and run:
                print("  driver exit code %s, output:\n%s" % (run.returncode, run.output[-4000:]))
                for request in run.requests:
                    body = json.dumps(request["body"])
                    print("  %s %s -> %s %s" % (request["path"], body[:300], request["status"], request["response"]))
        else:
            passed += 1
            print("PASS %-22s %.1fs" % (name, run.seconds), flush=True)
    print("%d passed, %d failed, %d skipped" % (passed, failed, skipped))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
