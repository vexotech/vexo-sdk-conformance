# Vexo SDK conformance

A shared test harness for the Vexo server-side SDKs (Node, Python, PHP, Ruby, Go, Java, and .NET). It checks
every SDK against the same contract:

- [`SPEC.md`](SPEC.md): the contract as numbered, testable requirements (wire format, the API's rules, and client
  behavior).
- `server.py`: a mock of the Vexo capture API that accepts and rejects requests the way the real API does,
  records them, and injects faults.
- `run.py`: runs the scenarios. For each one it starts from a clean mock, runs your SDK's driver, and checks what
  the mock received.

Both scripts need only Python 3.8 or later and its standard library.

## Run the scenarios

```bash
python3 run.py --driver "node /path/to/vexo-node/conformance/driver.mjs"
python3 run.py --driver "python3 /path/to/vexo-python/conformance/driver.py"
```

`run.py` prints `PASS` or `FAIL` per scenario and exits with a non-zero status when any scenario fails. Useful
options:

| Option | Description |
| --- | --- |
| `--list` | Lists the scenarios, the requirements each one checks, and what it does. |
| `--only a,b` | Runs only these scenarios. |
| `--skip a,b` | Skips these scenarios. Say why in your report (for example, `exit_hook` for a language without an exit hook). |
| `--verbose` | On a failure, prints the driver's output and the requests the mock received. |
| `--timeout 60` | Seconds before a driver run is killed and the scenario fails. |
| `--bind`, `--host-url` | Reach the mock from a Docker container (see [Run a driver in Docker](#run-a-driver-in-docker)). |

A full run takes about a minute, mostly in the scenarios that wait out backoff delays.

## Scenarios

| Scenario | Requirements | What it checks |
| --- | --- | --- |
| `basic` | W1, W2, M1-M3, M5, C16 | An identify and four captures go out in one batch with the right fields, integer epoch-ms timestamps (including one from the language's date-time type), unique message IDs, and a caller's own `messageId`. |
| `batch_size` | C3, C6 | With `flushAt` 3 and no `flush` call, the queue size alone sends the messages, in order, at most 3 per batch. |
| `batch_interval` | C4 | With `flushIntervalMs` 500 and no `flush` call, the timer sends the queue. |
| `retry_same_message_id` | M4, C7 | A batch that gets a `500` is resent with identical messages. |
| `retry_after` | C9 | A `429` with `Retry-After: 2` is retried after 2 seconds, not after the shorter backoff. |
| `backoff_5xx` | C7, C8 | A `503`, a dropped connection, and a `502` are retried after 0.5-1 s, 1-2 s, and 2-4 s. |
| `no_retry_on_4xx` | C10 | A `400` is reported with its status and isn't retried; the next batch goes out normally. |
| `bad_api_key` | A1, C10 | An unknown API key gets a `401`, which is reported and not retried. |
| `failed_resend` | M4, A15, C14 | A message the API lists in `failed` is resent alone with the same message ID. |
| `poison_isolated` | M8, M10, C15 | A message the JSON encoder rejects, one with a `NaN` value, and one the API drops are reported; the valid ones are stored and nothing is resent. |
| `split_5mb` | W5, C6, M9 | 100 messages of 60 KB are split into requests under 5 MB; one message over 5 MB is reported and never sent. |
| `shutdown_deadline` | C17 | Against a server that answers after 8 seconds, `shutdown` returns at its 1-second deadline and reports what it couldn't send. |
| `shutdown_inflight` | C17 | The same, but the SDK started the request on its own (`flushAt` 1) before `shutdown`: the deadline aborts that request too. |
| `shutdown_backoff` | C17 | The SDK is waiting out `Retry-After: 30` when `shutdown` is called: the deadline ends the wait, and no retry starts after it. |
| `unparseable_2xx` | W6 | A `200` whose body isn't the JSON envelope counts as accepted: no retry, no error. |
| `circuit_breaker` | C13 | After a batch uses up its retries, the next batch gets one attempt; a success restores the retries. |
| `queue_full` | C5 | With `maxQueueSize` 2, the third message is reported and dropped. |
| `exit_hook` | C19 | A program that queues a message and ends without `flush` or `shutdown` still sends it. |

Every scenario also checks each request's path, headers, batch size, message IDs, and timestamp types (W1, W2,
W4, M1-M3, M5). A capture's timestamp must be an integer, or a string when the caller passed one.

## Write a driver

A driver is a small program in your SDK's language that reads one scenario as JSON on stdin, calls your SDK, and
prints JSON lines on stdout. Keep it in your SDK's repo, for example at `conformance/driver.go`, and load the SDK
from the repo's source rather than from a registry. The Node and Python drivers are about 60 lines each:
`vexo-node/conformance/driver.mjs` and `vexo-python/conformance/driver.py`.

### Input

```json
{
  "host": "http://127.0.0.1:53111",
  "apiKey": "test-key",
  "options": { "flushAt": 3, "flushIntervalMs": 60000 },
  "steps": [
    { "op": "identify", "distinctId": "user-1", "traits": { "plan": "pro" } },
    { "op": "capture", "distinctId": "user-1", "event": "purchase", "properties": { "amount": 49.9 } },
    { "op": "flush" },
    { "op": "shutdown" }
  ]
}
```

Create one client with `apiKey`, `host`, and the options that are present, translated to your SDK's names and
units. Leave the others at your SDK's defaults:

| Option | Unit |
| --- | --- |
| `flushAt` | Messages |
| `flushIntervalMs` | Milliseconds |
| `maxRetries` | Retries |
| `maxQueueSize` | Messages |
| `timeoutMs` | Milliseconds |
| `shutdownTimeoutMs` | Milliseconds |

Pass an error callback that prints each error (see [Output](#output)).

### Steps

Run the steps in order:

| Step | What the driver does |
| --- | --- |
| `capture` | Calls `capture` with `distinctId`, `event`, and, when present, `properties`, `timestamp` (an epoch-ms number; pass it as your SDK's numeric timestamp), and `messageId`. Two fields need translating: `at` is an ISO 8601 string that you convert to your language's date-time type (`Date`, `datetime`, `time.Time`, `Instant`, `DateTimeOffset`, `DateTime`) and pass as the timestamp; `poison: true` means add a property named `poison` whose value your JSON encoder rejects (Node: `10n`; Python: a dict that contains itself; Go: a channel or a func; Ruby and PHP: invalid UTF-8 in a string; .NET and Java: whatever your serializer refuses, such as an object that refers to itself); `nan: true` means add a property named `nan` whose value is your language's floating-point NaN. |
| `identify` | Calls `identify` with `distinctId` and, when present, `traits`. |
| `flush` | Calls `flush` and waits for it to finish. |
| `shutdown` | Calls `shutdown` (or `close`) and waits for it to finish. |
| `sleep` | Sleeps for `ms` milliseconds. |
| `exit` | Ends the process at once with status 0, without flushing and without running exit hooks: `process.exit(0)` in Node, `os._exit(0)` in Python, `os.Exit(0)` in Go, `Runtime.getRuntime().halt(0)` in Java, `exit!(0)` in Ruby, `Environment.FailFast` or killing the current process in .NET, `posix_kill(getmypid(), SIGKILL)` in PHP. |

After the last step, return from `main` normally. Don't call `flush` or `shutdown` yourself: the `exit_hook`
scenario checks that your SDK's exit hook sends the queue.

### Output

Print one JSON object per line on stdout, and write it unbuffered (or flush after each line) so nothing is lost
when the `exit` step ends the process:

- After each step: `{"type": "step", "op": "<the step's op>", "ms": <how long the step took, in milliseconds>}`.
- Each time the SDK calls your error callback: `{"type": "error", "message": "<the error's message>", "status": <the HTTP status, or null>}`.

`run.py` ignores any other output, and prints it with `--verbose` when a scenario fails. When the callback can
run on another thread, guard the writes with a lock so lines don't interleave.

### Checklist

1. Run `python3 run.py --driver "<your command>" --verbose`.
2. Fix each failure in the SDK, not in the driver, unless the driver translated a step wrong.
3. Skip a scenario only when the requirement doesn't apply to your language, and say why: for example,
   `--skip exit_hook` for Go, or `--skip batch_interval` for a PHP SDK that has no timer and sends at the end of a
   request.

## Run a driver in Docker

When your language's toolchain runs in a container, bind the mock on all interfaces and give the driver the
host's address:

```bash
python3 run.py --bind 0.0.0.0 --host-url "http://host.docker.internal:{port}" \
  --driver "docker run --rm -i --init -v /path/to/vexo-go:/src -w /src golang:1.23 go run ./conformance/driver"
```

Keep `-i` so the container gets stdin, and `--init`: without it the driver runs as PID 1, which ignores a signal
it sends itself, such as the `SIGKILL` that a PHP driver's `exit` step uses. Build or download dependencies before the run (for example, in an image
you build once), because each scenario starts a new container. A slow start is fine: the timing checks measure
the time between requests and inside steps, not the driver's startup.

## Use the mock server on its own

Your SDK's unit tests can start the mock and drive it over HTTP:

```bash
python3 server.py --port 8765 --api-key test-key
```

| Endpoint | Description |
| --- | --- |
| `POST /v1/capture`, `/v1/identify`, `/v1/batch` | The capture API, with the real API's validation, status codes, and response envelope (SPEC.md, A1-A18). |
| `POST /_control/faults` | Queues faults. Each one applies to the next capture API request, in order. |
| `GET /_control/state` | Returns `{"requests": [...], "stored": [...], "pendingFaults": N}`. |
| `POST /_control/reset` | Forgets the requests, the stored messages, the faults, and the rate-limit window. |

A fault is a JSON object; `count` repeats it (default 1):

```json
{
  "faults": [
    { "status": 429, "retryAfter": 2 },
    { "status": 500, "count": 2 },
    { "delayMs": 5000 },
    { "reset": true },
    { "failIndexes": [1] },
    { "dropIndexes": [0] },
    {}
  ]
}
```

- `status`: answer with this status and the error envelope, plus `Retry-After` when `retryAfter` is set.
- `delayMs`: wait this long, then handle the request normally (or apply the fault's other fields).
- `reset`: close the connection without a response.
- `failIndexes`, `dropIndexes`: handle the request normally, but list these message indexes in `failed` or
  `dropped` instead of storing them.
- `{}`: no fault for this request; use it to let one request through between faults.

Each request record has `time` and `respondedAt` (Unix seconds), `path`, `headers` (lowercase names), `bytes`,
the parsed `body`, the `fault` it got, `status`, and the `response` body. Each stored message has its parsed
fields and `duplicate`, which is true when the API would ignore it as a repeated `messageId` (A18).

## What the mock doesn't model

- Account and events-cap limits (`406`, A2 and A9): inject them with `{"status": 406}`.
- `5xx` after a partial store (A17): injected `5xx` faults store nothing.
- The trimming in A10 uses Python's notion of whitespace, which also trims three no-break spaces that Java keeps.
