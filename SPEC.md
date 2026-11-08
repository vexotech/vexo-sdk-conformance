# Vexo server SDK contract

This is the contract every Vexo server-side SDK meets, written as numbered, testable requirements. It comes
from two sources:

- The API: vexo-api `origin/main`, under `src/main/java/com/vexo/restapi/`: `client/ServerCaptureResource.java`,
  `client/service/ServerCaptureService.java`, `client/service/ServerMessageParser.java`,
  `client/service/ServerIdentity.java`, `write/events/json/CustomEventValidator.java`,
  `api/interceptors/ApiKeyInterceptor.java`, `api/interceptors/ServerCaptureRateLimitInterceptor.java`, and
  `exception/GenericExceptionResponseHandler.java`. The public docs page is `app/server-side/capture/docs.mdx` in
  the docs repo.
- The reference SDKs: `vexo-node` and `vexo-python`.

The key words MUST, MUST NOT, SHOULD, and MAY mean what RFC 2119 says. "Report" means: call the SDK's error
callback (`onError`, `on_error`, or the language's equivalent) and never throw or raise to the caller. A
requirement that names a scenario is checked by `run.py`; the others are checked by each SDK's unit tests.

There are 59 requirements: W1-W6, M1-M11, A1-A18, C1-C24.

## Wire format (W)

- **W1.** The SDK sends messages with `POST {host}/v1/batch` and a UTF-8 JSON body `{"batch":[message, ...]}`. It
  doesn't use `/v1/capture` or `/v1/identify`, which take one message each. Scenario: `basic`.
- **W2.** Every request carries `X-Api-Key: <the app's API key>`, `Content-Type: application/json`, and
  `User-Agent: vexo-<language>/<sdk version>`, for example `vexo-node/0.1.0`. Scenario: every scenario.
- **W3.** `host` defaults to `https://api.vexo.co`. A trailing `/` on `host` is removed before the path is added.
- **W4.** A request holds at most 500 messages. Scenario: every scenario.
- **W5.** A request body is at most 5,242,880 bytes (5 MiB), measured as the bytes sent. Scenario: `split_5mb`.
- **W6.** The SDK reads the response body only on a `2xx` status, as the envelope in A15. A `2xx` means the API
  accepted the batch, so a `2xx` body it can't parse (a proxy's page, say) counts as a success with nothing
  dropped or failed: no retry and no error. Scenario: `unparseable_2xx`.

## Messages (M)

- **M1.** A capture message is `{"type":"capture","distinctId","event","properties","timestamp","messageId"}`.
  `properties` is a JSON object, `{}` when the caller passes none. Scenario: `basic`.
- **M2.** An identify message is `{"type":"identify","distinctId","traits","messageId"}`. `traits` is a JSON
  object, `{}` when the caller passes none. An identify has no `timestamp`. Scenario: `basic`.
- **M3.** The SDK sets `messageId` when the message is queued: a random UUID string, unless the caller passes a
  message ID, which the SDK sends unchanged. Scenario: `basic`.
- **M4.** The SDK sends the same `messageId`, and the same message body, on every retry of a message, including
  a `failed` resend (C14). Scenarios: `retry_same_message_id`, `failed_resend`.
- **M5.** `timestamp` is integer epoch milliseconds when the SDK produces it: the default (the time of the
  `capture` call), a value converted from the language's date-time type, or a number (any fraction is
  truncated). A date-time without a time zone is read as UTC. An SDK MAY also accept a string, which it sends
  unchanged; the API accepts ISO 8601 with an offset (A12). Scenario: `basic`.
- **M6.** `distinctId` and `event` are sent as strings. A capture without a `distinctId` or an `event`, or an
  identify without a `distinctId`, is reported and not queued.
- **M7.** A property or trait value that the JSON encoder can't handle but that has a conventional string form
  is sent as that string: decimals, UUIDs, and dates or date-times, in the forms that M11 sets.
- **M8.** The SDK serializes each message when it's queued. A message that still can't be serialized (for
  example a circular reference or a `BigInt`) is reported and not queued; no other message is affected. A
  message with a `NaN` or infinite number anywhere in it, a float or a decimal, is also reported and not queued:
  the API's parser refuses those tokens and would reject the whole batch with `400` (A5), and sending `null`
  instead (what `JSON.stringify` does) would change the caller's data silently. Scenario: `poison_isolated`.
- **M9.** A message whose serialized size plus `{"batch":[]}` is over 5,242,880 bytes is reported and not
  queued. Scenario: `split_5mb`.
- **M10.** The SDK MAY check the API's per-message rules (A11-A13) when a message is queued and report a message
  that breaks them instead of sending it. If it doesn't, the API's `dropped` result reports it (C15).
- **M11.** Every SDK encodes these property and trait values the same way, at any depth, so that the same value
  reads the same in Vexo whichever SDK sent it:
  1. A date-time is a string in UTC with milliseconds and `Z`, the form of JavaScript's
     `Date.prototype.toISOString()`: `2026-11-07T12:00:00.123Z`. A date-time with an offset is converted to UTC,
     and one without a time zone (Python's naive `datetime`, Java's `LocalDateTime`, a .NET `DateTime` of kind
     `Unspecified`) is read as UTC, as in M5. Digits after the milliseconds are dropped. A date without a time of
     day (Python's `date`, Java's `LocalDate`, .NET's `DateOnly`, Ruby's `Date`) is `YYYY-MM-DD`.
  2. An arbitrary-precision decimal (Python's `Decimal`, Java's `BigDecimal`, .NET's `decimal`, Ruby's
     `BigDecimal`, PHP's `BcMath\Number`) is a string in plain decimal notation with all of its digits, never in
     scientific notation, so no precision is lost: `Decimal("1.5E-7")` is `"0.00000015"`. Binary floating-point
     numbers stay JSON numbers.
  3. A string that isn't valid Unicode is sent, not dropped: each invalid UTF-8 sequence (PHP, Ruby, and Go strings
     hold bytes) or lone UTF-16 surrogate (Java and .NET) becomes U+FFFD. Node's `JSON.stringify` and Python's
     `json.dumps` write a lone surrogate as a `\uXXXX` escape instead, which JSON allows.
  4. A `NaN` or infinite number drops the message (M8).

  Scenario: `value_encoding`.

## API behavior (A)

The API checks a request in this order and stops at the first failure. `server.py` implements every rule here.

- **A1.** A request without `X-Api-Key` gets `401` with message `Missing X-Api-Key token`. A key that isn't
  valid gets `401` with message `Permission denied`. Scenario: `bad_api_key`.
- **A2.** An app whose account reached its event limit gets `406` before the body is read
  (`Account has reached event limit`).
- **A3.** Each app gets at most 1,200 requests in a clock minute across the three endpoints. Over that, the
  API returns `429` with a `Retry-After` header: whole seconds until the minute ends, at least 1. Message:
  `Rate limit exceeded for this app. Maximum 1200 requests per minute allowed.`
- **A4.** A body over 5,242,880 bytes gets `413`: `The request body exceeds 5 MB`.
- **A5.** A body that is empty or isn't valid JSON gets `400`: `The request body must be valid JSON`.
- **A6.** On `/v1/batch`, a body that isn't an object with a `batch` array gets `400`:
  `The body must be a JSON object with a batch array`.
- **A7.** An empty batch gets `400`: `Send at least one message`. More than 500 messages gets `400`:
  `A batch holds at most 500 messages`.
- **A8.** On `/v1/capture` and `/v1/identify` the body is one message, and the endpoint sets its `type`
  (overwriting any `type` in the body). On `/v1/batch`, a message without `type` is a capture.
- **A9.** When the batch has at least one valid capture and the app reached its events cap or the free-plan
  limit, the whole request gets `406` (`Account has reached events cap`, or
  `Free plan event limit reached. Add billing to keep collecting events.`). Nothing is stored.
- **A10.** Each message is checked on its own. A message that fails a check is listed in `dropped` with the
  reason below, and the other messages are unaffected. The checks, in order:
  1. Not a JSON object: `message must be a JSON object`.
  2. `type`, `distinctId`, `event`, or `messageId` present (not `null`) and not a string: `<field> must be a string`.
  3. `properties` or `traits` present and not an object: `<field> must be a JSON object`.
  4. `type` other than `capture` or `identify`: `type must be capture or identify`.
  5. `distinctId` missing or blank after trimming: `distinctId is required`.
  6. `distinctId` (trimmed) over 200 UTF-16 code units: `distinctId exceeds 200 characters`.
  7. An identify passes here; the rest apply to captures.
  8. `event` missing or blank: `event is required`.
  9. `event` over 255 UTF-16 code units: `event exceeds 255 characters`.
  10. `timestamp` that A12 rejects:
      `timestamp must be epoch milliseconds or an ISO 8601 date-time with an offset, no earlier than 2000-01-01T00:00:00Z`.
  11. Properties too large (A11): `Json node cannot exceed 65536 length`.
- **A11.** The API adds `"_routeName":"server"` to the properties (replacing any `_routeName`) and drops the
  event when the compact JSON of the result is over 65,536 UTF-16 code units. So the caller's properties can be
  at most 65,514 characters of compact JSON.
- **A12.** `timestamp` is optional (default: the time the API receives it). It's an integer of epoch
  milliseconds, a floating-point number (truncated toward zero), or an ISO 8601 date-time string with an offset
  (`Z`, `+02:00`, `+0200`, or `+02`), and it can't be earlier than `946684800000` (2000-01-01T00:00:00Z).
  Any other type is rejected.
- **A13.** A timestamp more than 1 hour after the API's clock is replaced with the time the API receives it.
- **A14.** An identify merges its `traits` into the person's traits; a trait set to `null` is removed.
- **A15.** A request that passes A1-A9 gets `200` with
  `{"success":true,"code":200,"message":null,"data":{"accepted":N,"dropped":[{"index":I,"reason":R}],"failed":[{"index":I,"reason":"Storing failed; retry this message"}]}}`.
  `index` is the message's 0-based position in the request; `dropped` and `failed` are sorted by `index`;
  `accepted` is the message count minus both lists. Scenario: `failed_resend`.
- **A16.** A `failed` message wasn't stored because of a temporary error; retrying it with the same `messageId`
  is safe. A `dropped` message is invalid and is never stored, however often it's sent.
- **A17.** A request rejected as a whole gets `{"success":false,"code":<status>,"message":<text>,"data":null}`.
  After any status except `5xx` the API stored none of the request's messages; after a `5xx` it may have stored
  some.
- **A18.** The API stores a capture's `messageId` (scoped to the app) for 5 days and ignores a capture whose
  `messageId` it already stored, still counting it as `accepted`. A blank `messageId` isn't deduplicated.

## Client behavior (C)

- **C1.** `capture` and `identify` only queue the message. They never wait for the network and never throw or
  raise on bad input: they report it and drop that one message.
- **C2.** Options, translated to the language's naming conventions, with these defaults: API key (required; the
  constructor MAY throw when it's missing, as the reference SDKs do), `host` (`https://api.vexo.co`), `flushAt`
  (100, clamped to 1-500), `flushInterval` (10 seconds; 0 turns the timer off, C4), `maxRetries` (3),
  `maxQueueSize` (10,000), `timeout` (10 seconds), `shutdownTimeout` (10 seconds), and an error callback
  (default: log the error).
- **C3.** When the queue reaches `flushAt` messages, the SDK starts sending without waiting for the interval or
  a `flush` call. Scenario: `batch_size`.
- **C4.** Every `flushInterval`, the SDK sends whatever is queued. A `flushInterval` of 0 turns the interval timer
  off, and never means "send at once" or a busy loop: messages are then sent on `flushAt`, `flush`, and `shutdown`
  only. In Go, where a zero `Config` field selects its default, a negative `FlushInterval` turns the timer off.
  Scenarios: `batch_interval`, `interval_off`.
- **C5.** The queue holds at most `maxQueueSize` messages. When it's full, the new message is reported and
  dropped; queued messages are kept. Scenario: `queue_full`.
- **C6.** A batch takes messages in queue order: at most `flushAt` of them, and only as many as fit in one body
  under W5. One message is never split across requests. Scenarios: `batch_size`, `split_5mb`.
- **C7.** A request that fails with a network error, a timeout, `429`, or `5xx` is retried, up to `maxRetries`
  retries (`maxRetries + 1` attempts). Scenarios: `retry_same_message_id`, `backoff_5xx`.
- **C8.** The wait before retry `n` (0 for the first retry) is random in `[b/2, b]`, where
  `b = min(1 second x 2^n, 30 seconds)`. Scenario: `backoff_5xx`.
- **C9.** When a retried response has a `Retry-After` header in seconds, the SDK waits that long instead of the
  backoff, capped at 60 seconds. Scenario: `retry_after`.
- **C10.** Any other `4xx` (including `400`, `401`, `406`, and `413`) isn't retried. The SDK reports it with the
  HTTP status and the batch's messages, and moves on to the next batch. Scenarios: `no_retry_on_4xx`,
  `bad_api_key`.
- **C11.** A batch that still fails after its last retry is reported with the batch's messages, and with the
  HTTP status when there was a response.
- **C12.** Each request is aborted after `timeout` and counts as a network error (C7).
- **C13.** Circuit breaker: once a batch uses up its retries on network errors, `429`, or `5xx`, later batches
  get a single attempt each, until a request succeeds (any `2xx`), which restores the normal retries.
  Scenario: `circuit_breaker`.
- **C14.** After a `200`, the SDK resends only the messages listed in `failed` (same `messageId` and body), with
  the backoff of C8, counting toward the same `maxRetries`. When the retries run out, it reports them with the
  API's reason. Scenario: `failed_resend`.
- **C15.** The SDK reports the messages listed in `dropped`, with the API's reason, and never resends them.
  Scenario: `poison_isolated`.
- **C16.** `flush` sends everything queued when it's called and returns (or its promise resolves) when that's
  done. Failures are reported, not thrown. Scenario: `basic`.
- **C17.** `shutdown` (or `close`) stops the interval timer and sends everything queued, giving up at
  `shutdownTimeout`. At the deadline it aborts the request in flight and ends any backoff wait, including a
  request or wait that the background sender started before `shutdown` was called, starts no new attempt, and
  reports the messages it didn't send. It returns within `shutdownTimeout` plus scheduling slack. Calling it
  again is safe. Scenarios: `shutdown_deadline`, `shutdown_inflight`, `shutdown_backoff`.
- **C18.** After `shutdown`, `capture` and `identify` report the message and drop it.
- **C19.** The SDK wires `shutdown` to the language's normal exit hook where one exists (`atexit`, Node's
  `beforeExit`, a JVM shutdown hook, `register_shutdown_function`, `IAsyncDisposable`) and never holds the
  process past `shutdownTimeout`. Scenario: `exit_hook` (skip it for a language without such a hook, such as Go,
  and document `defer client.Close()` instead).
- **C20.** The client is safe for concurrent callers. Where the runtime forks workers (gunicorn `--preload`,
  Celery prefork, Puma, Unicorn), the child starts its own sender after the fork.
- **C21.** Error reports carry the affected messages and, when there is one, the HTTP status (C10, C11) or the
  API's reason per message (C14, C15).
- **C22.** Where the normal deployment has no long-lived process (PHP requests, serverless functions, edge
  runtimes), the README shows how to flush per request. The interval timer can be turned off there (C4).
- **C23.** The SDK has no runtime dependencies unless the language lacks a standard HTTP client or JSON
  support; it documents any dependency it takes.
- **C24.** Importing or loading the SDK has no side effects beyond defining it: no timers, threads, or exit
  hooks until a client is created.
