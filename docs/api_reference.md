# API Reference

Base URL: `http://localhost:8000`

Interactive docs (Swagger UI): [http://localhost:8000/docs](http://localhost:8000/docs)

---

## Glossary of LLM Inference Terms

Before exploring the endpoints, it helps to understand the domain-specific terms used in the API:

* **`prompt_tokens` (Prefill phase)**: The number of tokens in the input prompt. The model processes these all at once, requiring an immediate chunk of KV-cache memory allocated upfront before generation can start.
* **`max_tokens` (Decode phase)**: The generation budget. During decoding, the model generates one token at a time, requiring memory to grow iteratively.
* **`priority`**: A weight used by the scheduler. When VRAM is full, lower-priority requests are preempted to make room for higher-priority ones.
* **`allocated_blocks`**: The physical footprint of a request. The number of fixed-size KV-cache blocks currently held by the request in simulated GPU VRAM.
* **`PREEMPTED` vs `SWAPPED`**: When VRAM saturates, requests are evicted. If `RESUME_MODE=recompute`, the request's state is discarded and its status becomes `PREEMPTED`. If `RESUME_MODE=swap`, the state is saved to disk/MongoDB (simulating CPU RAM) and its status becomes `SWAPPED`. Both modes free up GPU VRAM blocks for other requests.

---

## POST `/requests`

Submit a new simulated inference request. The request is immediately written to MongoDB with status `QUEUED` and pushed to SQS for the scheduler to pick up.

### Request Body
```json
{
  "prompt_tokens": 256,
  "max_tokens": 512,
  "priority": 2
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `prompt_tokens` | int | ✅ | Simulated prompt length in tokens |
| `max_tokens` | int | ✅ | Maximum tokens to generate |
| `priority` | int | no (default: 1) | Higher = higher priority; used by `priority` preemption policy |

### Response `201 Created`
```json
{
  "request_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "QUEUED"
}
```

---

## GET `/requests`

List all requests, optionally filtered by status.

### Query Parameters
| Param | Values | Description |
|---|---|---|
| `status` | `QUEUED` `RUNNING` `PREEMPTED` `SWAPPED` `COMPLETED` `FAILED` | Filter by current state |

### Example
```
GET /requests?status=RUNNING
```

### Response `200 OK`
```json
[
  {
    "id": "3fa85f64-...",
    "submitted_at": "2026-07-06T15:09:00.123Z",
    "prompt_tokens": 256,
    "max_tokens": 512,
    "generated_tokens": 128,
    "priority": 2,
    "status": "RUNNING",
    "allocated_blocks": 24,
    "history": [...]
  }
]
```

---

## GET `/requests/{request_id}`

Get the full state and history of a single request.

### Response `200 OK`
```json
{
  "id": "3fa85f64-...",
  "submitted_at": "2026-07-06T15:09:00.123Z",
  "prompt_tokens": 256,
  "max_tokens": 512,
  "generated_tokens": 512,
  "priority": 2,
  "status": "COMPLETED",
  "allocated_blocks": 0,
  "history": [
    {"ts": "2026-07-06T15:09:00Z", "event": "SUBMITTED"},
    {"ts": "2026-07-06T15:09:01Z", "event": "ADMITTED",   "blocks": 16},
    {"ts": "2026-07-06T15:09:03Z", "event": "GREW",        "blocks": 17},
    {"ts": "2026-07-06T15:09:08Z", "event": "PREEMPTED",   "freed_blocks": 17},
    {"ts": "2026-07-06T15:09:09Z", "event": "SWAPPED_OUT"},
    {"ts": "2026-07-06T15:09:12Z", "event": "SWAPPED_IN",  "blocks": 18},
    {"ts": "2026-07-06T15:09:45Z", "event": "COMPLETED",   "total_generated": 512}
  ]
}
```

### Response `404 Not Found`
```json
{"detail": "Request not found"}
```

---

## GET `/status`

Current scheduler snapshot — use this to observe memory pressure in real time.

### Response `200 OK`
```json
{
  "request_counts": {
    "QUEUED":    3,
    "RUNNING":  12,
    "PREEMPTED": 0,
    "SWAPPED":   2,
    "COMPLETED": 8,
    "FAILED":    0
  },
  "memory": {
    "total_blocks":    512,
    "free_blocks":      28,
    "utilization_pct": 94.5
  },
  "ts": "2026-07-06T15:10:00.000Z"
}
```

---

## GET `/metrics`

Time-series snapshots from the `metrics_snapshots` MongoDB collection. Useful for plotting VRAM utilisation over time.

### Query Parameters
| Param | Default | Description |
|---|---|---|
| `last_n` | 100 | Number of most recent snapshots to return |

### Response `200 OK`
```json
{
  "count": 3,
  "snapshots": [
    {
      "ts": "2026-07-06T15:09:50Z",
      "queue_depth":    6,
      "running_count": 14,
      "swapped_count":  1,
      "free_blocks":   28,
      "total_blocks": 512,
      "utilization_pct": 94.53
    },
    {
      "ts": "2026-07-06T15:09:52Z",
      "queue_depth":    5,
      "running_count": 15,
      "swapped_count":  1,
      "free_blocks":    9,
      "total_blocks": 512,
      "utilization_pct": 98.24
    }
  ]
}
```

---

## GET `/admin/config`

View the current runtime simulation configuration.

### Response `200 OK`
```json
{
  "TOTAL_BLOCKS": 512,
  "BLOCK_SIZE_TOKENS": 16,
  "TOKENS_PER_STEP": 8,
  "TICK_INTERVAL_MS": 200,
  "PREEMPTION_POLICY": "priority",
  "RESUME_MODE": "swap",
  "SWAP_IN_LATENCY_MS": 300,
  "RECOMPUTE_PENALTY_FACTOR": 0.5
}
```

---

## PATCH `/admin/config`

Update one or more simulation parameters **at runtime** without restarting anything.

### Request Body
Send only the fields you want to change:
```json
{
  "PREEMPTION_POLICY": "footprint",
  "RESUME_MODE": "recompute"
}
```

### Validation
| Field | Allowed values |
|---|---|
| `PREEMPTION_POLICY` | `"priority"` or `"footprint"` |
| `RESUME_MODE` | `"swap"` or `"recompute"` |

### Response `200 OK`
```json
{
  "updated": {
    "PREEMPTION_POLICY": "footprint",
    "RESUME_MODE": "recompute"
  },
  "config": { ... full config ... }
}
```

### Response `400 Bad Request`
```json
{"detail": "PREEMPTION_POLICY must be 'priority' or 'footprint'"}
```

---

## POST `/admin/reset`

Hard-reset the simulation: clears all MongoDB collections and purges the SQS queue.

> ⚠️ **Destructive** — all request history, events, and metrics are permanently deleted.

### Response `200 OK`
```json
{
  "status": "reset complete",
  "ts": "2026-07-06T15:30:00.000Z"
}
```
