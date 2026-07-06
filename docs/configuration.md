# Configuration

All parameters are loaded from the `.env` file at startup. The scheduler worker and FastAPI gateway both read from `.env` via `python-dotenv`.

Most parameters can also be changed **at runtime** (without restarting) via `PATCH /admin/config`.

---

## Environment Variables

### MongoDB

| Variable | Required | Example | Description |
|---|---|---|---|
| `MONGO_URI` | ✅ | `mongodb+srv://user:pass@cluster0.xxxx.mongodb.net/` | MongoDB Atlas connection string |
| `MONGO_DB_NAME` | no | `kv_simulator` | Database name (default: `kv_simulator`) |

### SQS (LocalStack)

| Variable | Default | Description |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | `test` | Dummy value for LocalStack (not real AWS) |
| `AWS_SECRET_ACCESS_KEY` | `test` | Dummy value for LocalStack |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `SQS_ENDPOINT_URL` | `http://localstack:4566` | LocalStack SQS endpoint. Use `http://localhost:4566` when running outside Docker |
| `SQS_QUEUE_NAME` | `inference-requests` | Name of the SQS queue (created automatically on startup) |

### Simulation Parameters

| Variable | Default | Runtime-patchable | Description |
|---|---|---|---|
| `TOTAL_BLOCKS` | `512` | ✅ | Total simulated GPU KV-cache blocks. Lower values make memory pressure appear faster |
| `BLOCK_SIZE_TOKENS` | `16` | ✅ | Number of tokens represented by one block. Matches vLLM's default page size |
| `TOKENS_PER_STEP` | `8` | ✅ | Tokens added to `generated_tokens` each scheduler tick |
| `TICK_INTERVAL_MS` | `200` | ✅ | Scheduler loop cadence in milliseconds |
| `PREEMPTION_POLICY` | `priority` | ✅ | `priority` or `footprint` (see below) |
| `RESUME_MODE` | `swap` | ✅ | `swap` or `recompute` (see below) |
| `SWAP_IN_LATENCY_MS` | `300` | ✅ | Simulated cost of reading KV state back from "CPU RAM" during swap-in |
| `RECOMPUTE_PENALTY_FACTOR` | `0.5` | ✅ | Fraction of prefill time re-paid when a recomputed request resumes |

---

## Parameter Guide

### `TOTAL_BLOCKS`
Controls how much "VRAM" the simulated GPU has. A smaller pool makes preemption and queuing visible much faster.

| Value | Effect |
|---|---|
| `512` (default) | Comfortable for ~10–15 concurrent medium requests |
| `64` | Pressure is visible immediately with just 3–4 large requests |
| `1024` | Hard to saturate; good for demoing "idle compute" scenario |

### `BLOCK_SIZE_TOKENS`
How many tokens fit in one block. Affects how coarsely memory grows.

```
blocks_needed = ceil((prompt_tokens + generated_tokens) / BLOCK_SIZE_TOKENS)
```

Smaller values → more blocks needed → pool saturates faster.

### `TOKENS_PER_STEP` and `TICK_INTERVAL_MS`
Together these control the simulated decode speed:

```
Simulated tokens/second ≈ TOKENS_PER_STEP / (TICK_INTERVAL_MS / 1000)
                        = 8 / 0.2 = 40 tokens/second (default)
```

Increase `TOKENS_PER_STEP` or decrease `TICK_INTERVAL_MS` to make requests complete faster.

### `PREEMPTION_POLICY`

| Value | Strategy | Best for |
|---|---|---|
| `priority` | Evict the lowest-priority, oldest request | Fairness to high-priority workloads |
| `footprint` | Evict the request holding the most blocks | Maximum throughput under sustained pressure |

### `RESUME_MODE`

| Value | On preemption | On resume | Cost |
|---|---|---|---|
| `swap` | Save KV state to MongoDB | Reload from MongoDB + wait `SWAP_IN_LATENCY_MS` | I/O latency; no recompute |
| `recompute` | Discard KV state | Re-simulate prefill (sleep proportional to prompt length) | CPU time; no storage |

### `SWAP_IN_LATENCY_MS`
Only relevant when `RESUME_MODE=swap`. Simulates the time cost of moving KV tensor data from host memory back to GPU VRAM. In real vLLM this is a PCIe transfer.

### `RECOMPUTE_PENALTY_FACTOR`
Only relevant when `RESUME_MODE=recompute`. A factor of `0.5` means the scheduler sleeps for half the time it would take to do a full prefill. A factor of `1.0` means full prefill cost is re-paid.

---

## Updating Config at Runtime

Use `PATCH /admin/config` to change parameters without restarting:

```bash
# Switch to footprint-based preemption
curl -X PATCH http://localhost:8000/admin/config \
  -H "Content-Type: application/json" \
  -d '{"PREEMPTION_POLICY": "footprint"}'

# Switch to recompute mode and halve the tick interval
curl -X PATCH http://localhost:8000/admin/config \
  -H "Content-Type: application/json" \
  -d '{"RESUME_MODE": "recompute", "TICK_INTERVAL_MS": 100}'

# Shrink the block pool to force pressure faster
curl -X PATCH http://localhost:8000/admin/config \
  -H "Content-Type: application/json" \
  -d '{"TOTAL_BLOCKS": 64}'
```

> **Note:** `TOTAL_BLOCKS` changes take effect for new allocations only. Existing allocations are not revalidated when the pool size changes.

---

## Example `.env` File

```ini
# MongoDB Atlas
MONGO_URI=mongodb+srv://alice:secret@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority
MONGO_DB_NAME=kv_simulator

# SQS (LocalStack)
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
AWS_DEFAULT_REGION=us-east-1
SQS_ENDPOINT_URL=http://localstack:4566
SQS_QUEUE_NAME=inference-requests

# Simulation
TOTAL_BLOCKS=512
BLOCK_SIZE_TOKENS=16
TOKENS_PER_STEP=8
TICK_INTERVAL_MS=200
PREEMPTION_POLICY=priority
RESUME_MODE=swap
SWAP_IN_LATENCY_MS=300
RECOMPUTE_PENALTY_FACTOR=0.5
```
