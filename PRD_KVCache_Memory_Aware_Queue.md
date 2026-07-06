# PRD: KV-Cache Memory-Aware Request Queue (vLLM-Inspired Scheduler Simulator)

**Author:** [Your Name]
**Status:** Draft v1.0
**Last Updated:** July 2026
**Stack:** Python, FastAPI, Docker, AWS SQS (via LocalStack), MongoDB

---

## 1. Overview

Modern LLM inference servers (vLLM, TensorRT-LLM, etc.) do not schedule requests based on CPU/GPU compute availability the way traditional web servers do. Instead, the binding constraint is **GPU VRAM occupied by the KV-cache** — the per-token, per-layer attention memory that must stay resident for the lifetime of a request.

This project builds a **prototype simulator** that models this behavior end-to-end using tools the team already knows: FastAPI for the API/control plane, SQS (via LocalStack) as the ingress queue, MongoDB as the system-of-record for request/memory state, and Docker to package it all as a runnable demo.

The goal is **not** to run a real LLM. The goal is to faithfully simulate the *scheduling and memory-management problem* — admission, preemption, swapping, and resumption governed by a simulated block-based memory pool — and to make the behavior observable (via API + logs/metrics) so it clearly demonstrates the difference between compute-based and memory-based scheduling.

---

## 2. Problem Statement

Traditional queue/worker systems (e.g., a naive FastAPI + Celery setup) admit a request whenever a worker thread/process is free. This model is wrong for LLM inference:

- A GPU can be compute-idle yet **unable to admit a new request** because VRAM is fully occupied by KV-cache blocks from in-flight requests.
- Long-running generations can starve short requests if there's no notion of memory pressure or preemption.
- Without paging, memory is allocated in large contiguous chunks sized for a request's *worst-case* length, wasting VRAM through fragmentation.

vLLM solved this with **PagedAttention**: KV-cache is split into fixed-size blocks, allocated non-contiguously (like OS virtual memory pages), enabling fine-grained admission control, preemption, and swapping.

This prototype simulates that scheduling logic so the mechanism can be studied, demoed, and extended — without needing GPUs or real model weights.

---

## 3. Goals

1. Simulate GPU VRAM as a **fixed pool of discrete KV-cache blocks**.
2. Implement **admission control**: a request is only pulled from the queue and started if enough free blocks exist to admit it.
3. Implement **incremental allocation**: a running request consumes additional blocks as its "generated token count" grows, mimicking real KV-cache growth during decoding.
4. Implement **preemption**: under memory pressure, pause a running request and reclaim its blocks (either by swap-out or discard-and-recompute).
5. Implement **swap-out / swap-in**: move a paused request's simulated KV-cache to a "CPU RAM" store (MongoDB collection or in-memory dict) and restore it later.
6. Expose all of this via a **FastAPI control plane** with clear, inspectable state.
7. Use **SQS (LocalStack)** as the real ingress mechanism for incoming inference requests, decoupling submission from scheduling.
8. Persist all state, transitions, and metrics in **MongoDB** for inspection, replay, and demoing.
9. Produce **metrics** that make the compute-vs-memory scheduling difference visible (queue wait time, block utilization over time, number of preemptions/swaps, throughput).
10. Package everything with **Docker Compose** for a one-command demo (`docker compose up`).

## 4. Non-Goals

- No real LLM inference or GPU usage — everything is simulated with configurable timers/counters.
- No production-grade autoscaling, multi-tenant auth, or billing.
- No real network-distributed GPU cluster simulation (single simulated "device" is sufficient for v1).
- No frontend UI required for v1 (a simple `/status` dashboard endpoint returning JSON is enough; a minimal HTML/JS view is a stretch goal).

---

## 5. Users / Use Case

**Primary audience:** engineers, interviewers, or researchers who want to *see* why LLM serving needs memory-aware scheduling instead of compute-based scheduling — this is a teaching/demo/portfolio artifact, not a production scheduler.

**Core demo flow:**
1. User submits N simulated inference requests (varying prompt/output lengths) via an API, which pushes them to SQS.
2. A scheduler worker continuously tries to admit requests from SQS based on free KV-cache blocks.
3. As requests run, they consume more blocks over "simulated decode steps."
4. When the pool is under pressure, the scheduler preempts a request (swap-out), continues others, and later resumes the preempted one.
5. User queries `/metrics` and `/status` to see block occupancy over time, queue depth, wait times, and preemption events — demonstrating memory-bound behavior.

---

## 6. System Architecture

```
                ┌─────────────────────┐
   Client  ───► │   FastAPI Gateway    │  (submit request, query status/metrics)
                │  /submit  /status    │
                │  /metrics /admin     │
                └──────────┬───────────┘
                           │ enqueue (boto3 -> SQS)
                           ▼
                ┌─────────────────────┐
                │   SQS Queue          │  (LocalStack)
                │  "inference-requests"│
                └──────────┬───────────┘
                           │ poll
                           ▼
                ┌─────────────────────────────────────┐
                │        Scheduler / Worker Service     │
                │  ┌─────────────────────────────────┐  │
                │  │  Block Memory Manager (in-proc)   │  │
                │  │  - total_blocks                   │  │
                │  │  - free_blocks / allocated map     │  │
                │  │  - allocate() / free() / can_fit() │  │
                │  └─────────────────────────────────┘  │
                │  ┌─────────────────────────────────┐  │
                │  │  Scheduling Loop                 │  │
                │  │  - admission control              │  │
                │  │  - decode-step simulation          │  │
                │  │  - preemption policy               │  │
                │  │  - swap-out / swap-in              │  │
                │  └─────────────────────────────────┘  │
                └──────────────────┬────────────────────┘
                                   │ persist state/events
                                   ▼
                         ┌───────────────────┐
                         │     MongoDB        │
                         │  requests          │
                         │  block_ledger       │
                         │  events (audit log) │
                         │  metrics_snapshots   │
                         └───────────────────┘
```

**Deployment:** All four pieces (FastAPI gateway, scheduler worker, LocalStack, MongoDB) run as services in `docker-compose.yml`.

---

## 7. Core Simulation Model

### 7.1 Memory Model

- GPU VRAM is modeled as `TOTAL_BLOCKS` fixed-size blocks (config value, e.g., 512).
- Each block represents a fixed number of simulated tokens (e.g., `BLOCK_SIZE_TOKENS = 16`).
- A request's KV-cache footprint at any point = `ceil(current_sequence_length / BLOCK_SIZE_TOKENS)` blocks.
- Blocks are tracked as a simple free-list / bitmap; no need to model physical addresses, just counts + a mapping of `request_id -> [block_ids]`.

### 7.2 Request Lifecycle (State Machine)

```
QUEUED ──(admitted: enough free blocks)──► RUNNING
RUNNING ──(decode steps complete)──► COMPLETED (blocks freed)
RUNNING ──(memory pressure, preemption policy triggers)──► PREEMPTED
PREEMPTED ──(swap-out: blocks freed, state persisted to "CPU RAM")──► SWAPPED
SWAPPED ──(blocks available again, resumed)──► RUNNING (swap-in, KV recomputed or reloaded)
QUEUED ──(TTL exceeded / cancelled)──► FAILED
```

### 7.3 Admission Control Algorithm (Scheduler Loop, simplified)

```python
while True:
    # 1. Try to admit new requests from SQS if capacity allows
    for msg in poll_sqs(max_messages=10):
        req = parse_request(msg)
        needed_blocks = estimate_prefill_blocks(req.prompt_len)
        if memory_manager.can_fit(needed_blocks):
            memory_manager.allocate(req.id, needed_blocks)
            req.status = "RUNNING"
            persist(req)
            ack_sqs(msg)
        else:
            # leave message in queue (visibility timeout expires -> retried)
            requeue_or_delay(msg)

    # 2. Advance one "decode step" for each running request
    for req in running_requests():
        req.generated_tokens += TOKENS_PER_STEP
        extra_blocks = blocks_needed(req) - memory_manager.allocated(req.id)
        if extra_blocks > 0:
            if memory_manager.can_fit(extra_blocks):
                memory_manager.grow(req.id, extra_blocks)
            else:
                preempt_lowest_priority_request()  # frees blocks
        if req.generated_tokens >= req.max_tokens:
            memory_manager.free(req.id)
            req.status = "COMPLETED"
            persist(req)

    # 3. Try to resume any SWAPPED requests if room now exists
    for req in swapped_requests_by_priority():
        if memory_manager.can_fit(blocks_needed(req)):
            memory_manager.allocate(req.id, blocks_needed(req))
            req.status = "RUNNING"
            persist(req)

    sleep(TICK_INTERVAL)
```

### 7.4 Preemption Policy (v1: simple, pluggable)

- **Policy A (default): Preempt-lowest-priority** — evict the running request with the lowest priority score (e.g., FCFS timestamp, or explicit priority field).
- **Policy B (stretch): Preempt-largest-footprint** — evict whichever running request frees the most blocks fastest.
- Policy is a strategy object so it can be swapped without touching scheduler core logic — useful for demoing "here's how policy choice changes fairness/throughput."

### 7.5 Swap Mechanism

- **Swap-out:** on preemption, serialize the request's simulated KV state (`request_id`, `generated_tokens`, `block_count`) into a MongoDB `swapped_state` collection (representing "CPU RAM"); free its GPU blocks.
- **Swap-in:** when re-admitted, read back the swapped state and re-allocate the required blocks; add a configurable `SWAP_IN_LATENCY_MS` to simulate the real cost of moving data back to VRAM (this is what makes swapping "expensive" vs. discard-and-recompute).
- **Recompute alternative (config toggle):** instead of swap, discard the KV-cache entirely on preemption and recompute prefill from scratch on resume — cheaper on "CPU RAM" but pays a recompute penalty on resume. Exposing both lets the demo compare strategies.

---

## 8. Data Model (MongoDB Collections)

### `requests`
```json
{
  "_id": "req_1234",
  "submitted_at": "ISODate",
  "prompt_tokens": 128,
  "max_tokens": 256,
  "generated_tokens": 40,
  "priority": 1,
  "status": "RUNNING | QUEUED | PREEMPTED | SWAPPED | COMPLETED | FAILED",
  "allocated_blocks": 12,
  "history": [
    {"ts": "...", "event": "ADMITTED", "blocks": 8},
    {"ts": "...", "event": "GREW", "blocks": 12},
    {"ts": "...", "event": "PREEMPTED", "reason": "memory_pressure"},
    {"ts": "...", "event": "SWAPPED_OUT"},
    {"ts": "...", "event": "SWAPPED_IN"},
    {"ts": "...", "event": "COMPLETED"}
  ]
}
```

### `block_ledger`
```json
{
  "_id": "snapshot_ts",
  "total_blocks": 512,
  "free_blocks": 87,
  "allocated_by_request": {"req_1234": 12, "req_5678": 20},
  "timestamp": "ISODate"
}
```

### `events` (audit log, append-only)
```json
{"ts": "...", "request_id": "req_1234", "event_type": "ADMITTED", "detail": {...}}
```

### `metrics_snapshots` (for time-series charts)
```json
{"ts": "...", "queue_depth": 14, "running_count": 9, "free_blocks": 60, "utilization_pct": 88.3}
```

---

## 9. API Design (FastAPI)

| Endpoint | Method | Description |
|---|---|---|
| `/requests` | `POST` | Submit a simulated inference request `{prompt_tokens, max_tokens, priority}`; pushes to SQS, returns `request_id`. |
| `/requests/{id}` | `GET` | Get current status + history of a request. |
| `/requests` | `GET` | List requests, filterable by status. |
| `/status` | `GET` | Current scheduler snapshot: free blocks, running/queued/swapped counts. |
| `/metrics` | `GET` | Time-series metrics (queue depth, utilization %, preemption count) for charting. |
| `/admin/config` | `GET/PATCH` | View/update simulation parameters at runtime (`TOTAL_BLOCKS`, `BLOCK_SIZE_TOKENS`, preemption policy, swap vs. recompute mode). |
| `/admin/reset` | `POST` | Reset simulation state (clears Mongo collections, purges SQS queue) — useful for repeated demos. |

---

## 10. Tech Stack Mapping

| Component | Technology | Notes |
|---|---|---|
| API / control plane | FastAPI | Submit requests, expose status/metrics/admin endpoints |
| Ingress queue | AWS SQS via LocalStack | Real SQS semantics (visibility timeout as a natural "requeue if not admitted" mechanism) |
| Scheduler / worker | Python asyncio service (separate container) | Runs the admission + decode-step + preemption loop |
| State store | MongoDB | Requests, block ledger, event log, metrics snapshots |
| Orchestration | Docker Compose | One command spins up FastAPI, worker, LocalStack, MongoDB |
| Load generator (for demo) | Small Python script / `locust` (optional) | Fires a burst of requests with varied sizes to visibly trigger preemption |

**Why SQS visibility timeout matters here:** when the scheduler can't admit a message (not enough free blocks), it simply does *not* delete it from SQS — the message becomes visible again after the timeout and is retried automatically. This maps SQS's native redelivery mechanic directly onto "leave it in queue until memory frees up," which is a nice, low-code way to get realistic queuing behavior for free.

---

## 11. Metrics & Observability (what makes the demo compelling)

- **Block utilization over time** (line chart): shows VRAM saturating even while "compute" would be idle in a naive model.
- **Queue wait time distribution**: compare a "memory-aware" run vs. a "compute-only" baseline run (toggle via config) to show queued requests waiting despite free "workers."
- **Preemption/swap event count and their cost** (added latency from swap-in or recompute).
- **Throughput (completed requests / minute)** under different preemption policies and different `TOTAL_BLOCKS` settings.
- All snapshots stored in `metrics_snapshots`; a simple `/metrics` JSON response is sufficient for v1 — can be piped into a notebook or a lightweight chart for the presentation.

---

## 12. Milestones / Phased Plan

**Phase 1 — Core simulation (no infra)**
- Implement `BlockMemoryManager` and request state machine as pure Python + unit tests.
- Simple in-memory scheduler loop, prove admission/preemption/swap logic works via a CLI script.

**Phase 2 — Wire up infra**
- Add FastAPI gateway with `/requests`, `/status`.
- Add LocalStack SQS integration for ingestion.
- Add MongoDB persistence for requests/events.
- Dockerize all services; `docker compose up` runs the full stack.

**Phase 3 — Scheduler service + policies**
- Move scheduler loop into its own worker container polling SQS.
- Implement pluggable preemption policies (priority-based, footprint-based).
- Implement swap-out/swap-in and the recompute-vs-swap toggle.

**Phase 4 — Observability & demo polish**
- `/metrics` endpoint + metrics_snapshots collection.
- Load-generator script to produce a compelling burst scenario.
- README with a "compute-based vs memory-aware" side-by-side comparison run.
- (Stretch) minimal HTML dashboard (Chart.js) polling `/metrics`.

---

## 13. Success Criteria

- Running `docker compose up` + the load-generator script produces a visible, reproducible scenario where:
  - The block pool saturates before all "workers" would be busy in a naive model.
  - At least one request gets preempted and later successfully resumed with correct state.
  - `/metrics` clearly shows the difference in queue wait time between a memory-aware scheduling run and a compute-only baseline run (toggleable config).
- All state transitions are traceable end-to-end in MongoDB (`events` collection) for any given `request_id`.
- The prototype is understandable and demoable in under 5 minutes to someone unfamiliar with the project.

---

## 14. Configuration Parameters (all runtime-adjustable via `/admin/config`)

| Param | Default | Description |
|---|---|---|
| `TOTAL_BLOCKS` | 512 | Simulated total GPU KV-cache blocks |
| `BLOCK_SIZE_TOKENS` | 16 | Tokens represented per block |
| `TOKENS_PER_STEP` | 8 | Simulated tokens generated per scheduler tick |
| `TICK_INTERVAL_MS` | 200 | Scheduler loop cadence |
| `PREEMPTION_POLICY` | `priority` | `priority` \| `footprint` |
| `RESUME_MODE` | `swap` | `swap` \| `recompute` |
| `SWAP_IN_LATENCY_MS` | 300 | Simulated cost of swap-in |
| `RECOMPUTE_PENALTY_FACTOR` | 0.5 | Fraction of prefill time re-paid on recompute-resume |

---

## 15. Risks / Open Questions

- **Fidelity vs. simplicity tradeoff:** how closely should token/block math mirror real vLLM internals vs. staying simple enough to build in scope? (v1 leans simple — linear block growth per generated token, no attention-layer-level detail.)
- **SQS visibility-timeout tuning:** need to pick values that make retries visible in a demo timeframe without excessive polling cost against LocalStack.
- **Concurrency correctness:** the in-memory `BlockMemoryManager` must be safe under the async scheduler loop (single-writer is fine for v1; no need for distributed locking since there's one scheduler instance).

---

## 16. Future Extensions (out of scope for v1)

- Multiple simulated GPUs / tensor-parallel block pools.
- Continuous batching simulation (mixing prefill and decode steps within a tick, as real vLLM does).
- Real cost modeling using actual model configs (num layers, hidden size, dtype) to compute realistic block sizes.
- Web dashboard with live-updating charts instead of polling `/metrics`.
- Chaos-testing mode: randomly kill the scheduler mid-run to test recovery from MongoDB state.
