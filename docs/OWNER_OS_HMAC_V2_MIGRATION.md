# Moving API clients from legacy HMAC to v2

Status: legacy HMAC is still accepted by default. Nothing here changes that.

## The risk
A legacy signature covers only the timestamp. One captured header pair works on any endpoint with any body for the whole replay window. It cannot decide fullcycle reviews, which need a client token.

**External clients are unknown.** Nothing in this repo signs HMAC outside tests. That says nothing about clients elsewhere, so assume some exist until the `warn` step shows otherwise. Turning legacy off with a client still using it breaks that client with 401.

## Client contract (v2)
Send `X-Runtime-Timestamp` (Unix seconds), `X-Runtime-Signature-Version: 2` and `X-Runtime-Signature`.

The signature is the lowercase hex HMAC-SHA256, keyed with the runtime token, of this compact ASCII JSON array:

    ["v2","<timestamp>","<METHOD>","<path>","<query>","<sha256 hex of the raw body>"]

- `path` and `query` are the request target exactly as sent: percent-encoding kept, query without the leading `?`, empty string when there is none.
- Reference: `hmac_v2_message` in `api/v1.py`. JSON escaping must match Python's `json.dumps` with `ensure_ascii=True` and `separators=(",", ":")`.
- A proxy in front must not rewrite the path or query.
- Each signature works once, inside the replay window. Sign every request again, including retries.

## Migration steps
Each step is the owner's decision, and each is undone by restoring the previous value and restarting the service.

| step | setting | effect |
|---|---|---|
| 1. Find clients | `RUNTIME_HMAC_LEGACY=warn` | still accepted; each use logs `legacy_hmac_accepted client=… path=…` |
| 2. Move clients | same | clients change to v2 |
| 3. Watch | same | run long enough to cover the rarest client; no new log lines |
| 4. Switch off | `RUNTIME_HMAC_LEGACY=0` | legacy gets 401 `legacy signature disabled` |

## Limits
- Counters and the v2 replay cache are per process, and a restart clears them.
- Bearer authentication is separate and unaffected.
