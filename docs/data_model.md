# Data Model

All state is persisted in MongoDB Atlas. The database name is `kv_simulator` (configurable via `MONGO_DB_NAME` in `.env`).

---

## Collection: `requests`

The primary collection. One document per submitted request, updated in-place as the request moves through its lifecycle.

### Schema

```json
{
  "_id":              "ObjectId (auto)",
  "id":               "req-3fa85f64-...",
  "submitted_at":     "2026-07-06T15:09:00.123Z",
  "prompt_tokens":    256,
  "max_tokens":       512,
  "generated_tokens": 128,
  "priority":         2,
  "status":           "RUNNING",
  "allocated_blocks": 24,
  "history": [
    {"ts": "...", "event": "SUBMITTED"},
    {"ts": "...", "event": "ADMITTED",   "blocks": 16},
    {"ts": "...", "event": "GREW",        "blocks": 17},
    {"ts": "...", "event": "SWAPPED_OUT", "freed_blocks": 17},
    {"ts": "...", "event": "SWAPPED_IN",  "blocks": 17},
    {"ts": "...", "event": "COMPLETED",   "total_generated": 512}
  ]
}
```

### Field Reference

| Field | Type | Description |
|---|---|---|
| `id` | string | UUID generated at submission time; used in all API responses |
| `submitted_at` | ISO datetime | When the client called `POST /requests` |
| `prompt_tokens` | int | Simulated prompt length; drives initial block allocation |
| `max_tokens` | int | Target number of tokens to generate; request completes when `generated_tokens >= max_tokens` |
| `generated_tokens` | int | Incremented by `TOKENS_PER_STEP` each scheduler tick while RUNNING |
| `priority` | int | Higher number = higher priority in the `priority` preemption policy |
| `status` | enum | One of `QUEUED`, `RUNNING`, `PREEMPTED`, `SWAPPED`, `COMPLETED`, `FAILED` |
| `allocated_blocks` | int | Current number of KV-cache blocks held; 0 when not RUNNING |
| `history` | array | Append-only log of every event this request experienced |

### History Event Types

| Event | When emitted | Extra fields |
|---|---|---|
| `SUBMITTED` | On `POST /requests` | — |
| `ADMITTED` | Scheduler: request moved from SQS to RUNNING | `blocks` (initial allocation) |
| `GREW` | Scheduler: request allocated more blocks during decode | `blocks` (new total) |
| `PREEMPTED` | Scheduler: evicted from RUNNING in recompute mode | `freed_blocks`, `mode: "recompute"` |
| `SWAPPED_OUT` | Scheduler: evicted from RUNNING in swap mode | `freed_blocks` |
| `SWAPPED_IN` | Scheduler: resumed from SWAPPED state | `blocks` |
| `RECOMPUTED` | Scheduler: resumed from PREEMPTED state (recompute mode) | `blocks` |
| `RESUMED` | Scheduler: general resume event written to `events` collection | `blocks`, `mode` |
| `COMPLETED` | Scheduler: all tokens generated | `total_generated` |

---

## Collection: `events`

Append-only audit log. Every scheduler action appends a document here — even if the same event is also written into the request's `history` array. Useful for cross-request analysis (e.g., "show me all preemptions in the last minute").

### Schema

```json
{
  "_id":        "ObjectId (auto)",
  "ts":         "2026-07-06T15:09:38Z",
  "request_id": "req-3fa85f64-...",
  "event_type": "ADMITTED",
  "detail": {
    "blocks": 16
  }
}
```

### Querying Examples

```javascript
// All preemption events
db.events.find({ event_type: "SWAPPED_OUT" }).sort({ ts: -1 })

// Full event history for a single request
db.events.find({ request_id: "req-3fa85f64-..." }).sort({ ts: 1 })

// Events in the last 60 seconds
db.events.find({ ts: { $gte: new Date(Date.now() - 60000).toISOString() } })
```

---

## Collection: `metrics_snapshots`

Time-series snapshots written at the end of every scheduler tick. Used by the `/metrics` API endpoint and suitable for feeding into a charting tool.

### Schema

```json
{
  "_id":             "ObjectId (auto)",
  "ts":              "2026-07-06T15:09:52Z",
  "queue_depth":     5,
  "running_count":   15,
  "swapped_count":   1,
  "free_blocks":     9,
  "total_blocks":    512,
  "utilization_pct": 98.24,
  "preemption_count": 0
}
```

### Field Reference

| Field | Description |
|---|---|
| `ts` | ISO timestamp of the snapshot |
| `queue_depth` | Number of SQS messages returned in this tick's poll (approximate queue depth) |
| `running_count` | RUNNING requests at time of snapshot |
| `swapped_count` | SWAPPED requests at time of snapshot |
| `free_blocks` | Unallocated blocks in the pool |
| `total_blocks` | Configured pool size (constant unless `/admin/config` is patched) |
| `utilization_pct` | `(total_blocks - free_blocks) / total_blocks * 100` |
| `preemption_count` | Reserved; always 0 in v1 (preemption count per tick is tracked in worker logs) |

---

## Collection: `swapped_state`

Simulates "CPU RAM" for the swap mode. When a request is preempted with `RESUME_MODE=swap`, its KV state is written here. When the request resumes, this document is read and then the request continues.

### Schema

```json
{
  "_id":             "ObjectId (auto)",
  "request_id":      "req-3fa85f64-...",
  "generated_tokens": 128,
  "block_count":      17,
  "swapped_at":      "2026-07-06T15:09:38Z"
}
```

Documents in this collection are upserted on each swap-out (keyed by `request_id`), so only the most recent swap state is kept per request.

> In a real system this would be a host-memory buffer. Here it's MongoDB, which makes it inspectable and durable across scheduler restarts.
