# Architecture

## Overview

The simulator is split into four independent services, each with a single responsibility. They communicate through MongoDB Atlas (shared state) and SQS (message passing).

```
                 ┌──────────────────────────────────┐
   HTTP Client ─►│         FastAPI Gateway           │
                 │  POST /requests                   │
                 │  GET  /status  /metrics           │
                 │  PATCH /admin/config              │
                 └──────────────┬───────────────────┘
                                │ 1. insert doc (QUEUED)
                                │ 2. send SQS message
                                ▼
                 ┌──────────────────────────────────┐
                 │        SQS Queue (LocalStack)     │
                 │     "inference-requests"          │
                 └──────────────┬───────────────────┘
                                │ poll (long-poll, 1s)
                                ▼
                 ┌──────────────────────────────────────────┐
                 │         Scheduler Worker                  │
                 │                                           │
                 │  ┌────────────────────────────────────┐  │
                 │  │       BlockMemoryManager            │  │
                 │  │  total_blocks / free_blocks         │  │
                 │  │  allocated_by_request: {id: count} │  │
                 │  │  allocate / free / grow / can_fit  │  │
                 │  └────────────────────────────────────┘  │
                 │                                           │
                 │  Per tick (TICK_INTERVAL_MS):             │
                 │    1. Admit from SQS (if blocks free)     │
                 │    2. Advance decode step for RUNNING     │
                 │    3. Preempt if memory pressure          │
                 │    4. Resume SWAPPED if room available    │
                 │    5. Snapshot metrics                    │
                 └──────────────┬────────────────────────────┘
                                │ persist all state & events
                                ▼
                 ┌──────────────────────────────────┐
                 │         MongoDB Atlas             │
                 │                                   │
                 │  requests          (per-request)  │
                 │  events            (audit log)    │
                 │  metrics_snapshots (time-series)  │
                 │  swapped_state     (CPU-RAM sim)  │
                 └──────────────────────────────────┘
```

---

## Component Roles

### FastAPI Gateway (`src/api/main.py`)
- The only component clients talk to directly
- Writes request documents to MongoDB with status `QUEUED`
- Publishes a JSON message to SQS — this decouples submission from scheduling
- Exposes read-only views of scheduler state via `/status` and `/metrics`
- Exposes `/admin/config` and `/admin/reset` for runtime control

### SQS Queue (LocalStack)
- Acts as a durable ingress buffer between submission and scheduling
- The scheduler does **not** `DeleteMessage` if it can't admit a request (not enough free blocks). The SQS visibility timeout expires and the message reappears automatically — this is "requeue for free" using SQS native semantics
- Decouples the gateway from the worker; either can restart independently

### Scheduler Worker (`src/worker/scheduler.py`)
- Runs as a continuous asyncio loop
- Owns the in-memory `BlockMemoryManager` — the single source of truth for block allocation
- Persists every state change to MongoDB so the gateway can serve accurate status
- Pluggable preemption policy (`priority` or `footprint`) and resume mode (`swap` or `recompute`)

### MongoDB Atlas
- Stores all durable state: request documents, event audit log, block ledger snapshots, swapped KV state
- The FastAPI gateway reads from here to serve `/status`, `/metrics`, `/requests`
- On crash/restart, the scheduler can recover in-flight state from MongoDB

---

## Data Flow: Request Lifecycle

```
Client
  │
  │  POST /requests {prompt_tokens, max_tokens, priority}
  ▼
FastAPI
  │── writes {status: QUEUED} to MongoDB requests collection
  │── sends JSON message to SQS
  │── returns {request_id, status: QUEUED}
  │
SQS (message visible)
  │
Scheduler tick
  │── polls SQS (up to 10 messages)
  │── for each message:
  │     needed_blocks = ceil((prompt_tokens) / BLOCK_SIZE_TOKENS)
  │     if memory_manager.can_fit(needed_blocks):
  │       allocate blocks
  │       set status = RUNNING
  │       DeleteMessage from SQS
  │     else:
  │       leave message (visibility timeout → retry)
  │
  │── for each RUNNING request:
  │     generated_tokens += TOKENS_PER_STEP
  │     extra_blocks = blocks_needed_now - currently_allocated
  │     if extra_blocks > 0 and pool has room → grow
  │     if extra_blocks > 0 and pool is full  → preempt a victim
  │     if generated_tokens >= max_tokens     → free blocks, COMPLETED
  │
  │── for each SWAPPED request:
  │     if pool has room → re-allocate, RUNNING (swap-in or recompute)
  │
  └── write metrics snapshot
```

---

## Deployment

All services are orchestrated with Docker Compose. MongoDB Atlas is external (cloud-managed); only LocalStack + the two Python services run locally.

```yaml
services:
  localstack:   # SQS
  api:          # FastAPI, port 8000
  worker:       # Scheduler loop
```

Both `api` and `worker` use the same `Dockerfile` and the same Python image, but different entrypoint commands. They share the codebase via a volume mount in development (`- .:/app`).
