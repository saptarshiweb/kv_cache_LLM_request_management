# Scheduler Internals

This document explains how the scheduler loop works, including the memory model, admission control, decode-step simulation, preemption policies, and swap/recompute mechanisms.

---

## 1. Memory Model

GPU VRAM is modelled as a **fixed pool of discrete blocks**. There are no physical addresses — just a count and a map.

```
TOTAL_BLOCKS = 512         # total simulated VRAM blocks
BLOCK_SIZE_TOKENS = 16     # how many tokens one block can hold

BlockMemoryManager state:
  free_blocks: int                       # blocks not yet allocated
  allocated_by_request: {req_id: int}    # how many blocks each request holds
```

**Block demand formula:**
```
blocks_needed = ceil((prompt_tokens + generated_tokens) / BLOCK_SIZE_TOKENS)
```

A request starts by claiming blocks for its prompt (prefill), then grows one or more blocks at a time as decoding produces new tokens.

---

## 2. The Scheduler Loop

The scheduler runs as an `asyncio` coroutine that ticks every `TICK_INTERVAL_MS` milliseconds. Each tick has five phases:

```
┌─────────────────────────────────────────────────────────────┐
│  TICK (every TICK_INTERVAL_MS ms)                           │
│                                                             │
│  Phase 1 — ADMIT                                            │
│    Poll SQS for up to 10 messages                           │
│    For each: check can_fit() → allocate + RUNNING           │
│              or leave in queue (visibility timeout retries) │
│                                                             │
│  Phase 2 — DECODE STEP                                      │
│    For each RUNNING request:                                │
│      generated_tokens += TOKENS_PER_STEP                   │
│      Compute extra blocks needed                            │
│      If pool has room → grow()                              │
│      If pool is full  → preempt a victim, then try again   │
│      If done (generated >= max_tokens) → free + COMPLETED  │
│                                                             │
│  Phase 3 — RESUME                                           │
│    For each SWAPPED request (sorted by priority):           │
│      If can_fit() → allocate + RUNNING                      │
│        (with simulated latency: SWAP_IN or RECOMPUTE)       │
│                                                             │
│  Phase 4 — METRICS SNAPSHOT                                 │
│    Write free_blocks, utilization%, counts to MongoDB       │
│                                                             │
│  sleep(TICK_INTERVAL_MS / 1000)                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Request State Machine

```
                   ┌──────────────────────────────────────┐
                   │                                      │
  [submitted] ──► QUEUED ──(enough blocks)──────────► RUNNING ──(done)──► COMPLETED
                    │                                    │
                    │                          (memory pressure)
                    │                                    │
                    │                                    ▼
                    │                               PREEMPTED ◄─── (recompute mode)
                    │                                    │
                    │                        (swap mode) │
                    │                                    ▼
                    │                                SWAPPED ──(blocks free)──► RUNNING
                    │
                    └──(TTL exceeded)──► FAILED
```

| State | Meaning |
|---|---|
| `QUEUED` | In SQS, waiting for enough free blocks |
| `RUNNING` | Actively consuming blocks; decode-step advancing each tick |
| `PREEMPTED` | Evicted in recompute mode; KV state discarded; blocks freed |
| `SWAPPED` | Evicted in swap mode; KV state persisted to MongoDB; blocks freed |
| `COMPLETED` | All tokens generated; blocks returned to pool |
| `FAILED` | TTL exceeded or error |

---

## 4. Preemption

Preemption fires when a RUNNING request needs more blocks but `can_fit()` returns `False`.

The scheduler calls `_pick_victim()` to choose which request to evict:

### Policy A: `priority` (default)
Evict the request with the **lowest priority score**, breaking ties by submission time (oldest first).
```python
victim = min(running, key=lambda r: (r.priority, r.submitted_at))
```
This is fair to high-priority requests but may repeatedly preempt the same long-running low-priority job.

### Policy B: `footprint`
Evict the request currently holding the **most blocks** — maximises the memory freed per preemption event.
```python
victim = max(running, key=lambda r: memory_manager.allocated(r.id))
```
Better throughput under sustained pressure; may starve large requests.

Switch policies at runtime without restarting:
```
PATCH /admin/config   {"PREEMPTION_POLICY": "footprint"}
```

---

## 5. Swap vs Recompute

When a request is preempted its blocks are freed. What happens to its KV state depends on `RESUME_MODE`:

### `swap` (default)
- On preemption: serialise `{request_id, generated_tokens, block_count}` into MongoDB `swapped_state` collection (simulating CPU RAM)
- On resume: read back the state, re-allocate blocks, sleep `SWAP_IN_LATENCY_MS` to simulate data transfer cost, then continue from where it left off
- **Cost:** swap-in latency (configurable, default 300ms); no recompute
- **Benefit:** no tokens are re-generated; the request resumes exactly where it was

### `recompute`
- On preemption: discard KV state entirely; status goes to `PREEMPTED`
- On resume: re-allocate blocks, sleep `prompt_tokens / TOKENS_PER_STEP * tick_interval * RECOMPUTE_PENALTY_FACTOR` to simulate re-running the prefill
- **Cost:** proportional to prompt length; longer prompts pay more on resume
- **Benefit:** no MongoDB storage for swapped state; simpler bookkeeping

Switch modes at runtime:
```
PATCH /admin/config   {"RESUME_MODE": "recompute"}
```

---

## 6. SQS Visibility Timeout as Natural Requeue

A key design decision: when the scheduler polls a message it **cannot** admit (not enough free blocks), it simply does *not* call `DeleteMessage`. The SQS visibility timeout (30 seconds) then expires and the message becomes visible again automatically.

This means:
- No explicit "requeue" logic is needed
- The request naturally retries every ~30 seconds until memory is available
- If the scheduler crashes, messages reappear and are reprocessed — durable by default

---

## 7. Concurrency Safety

The in-memory `BlockMemoryManager` is intentionally single-writer:
- There is exactly one scheduler worker instance
- The asyncio event loop is single-threaded; there is no concurrent mutation
- No locking is required for v1

If you scale to multiple scheduler workers in future, you would need to move block tracking into MongoDB with optimistic locking or a Redis counter.
