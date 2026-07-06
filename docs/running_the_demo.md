# Running the Demo

This walkthrough takes you from a cold start to a live simulation with visible preemptions, swap events, and memory saturation — in about 5 minutes.

---

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- A MongoDB Atlas cluster (free M0 tier is fine) — [create one here](https://cloud.mongodb.com/)
- Python 3.11+ with `pip` available locally (for the load generator only)

---

## Step 1 — Configure Secrets

Copy the example env file and fill in your Atlas URI:

```bash
cp .env.example .env
```

Open `.env` and set:
```ini
MONGO_URI=mongodb+srv://<username>:<password>@cluster0.xxxx.mongodb.net/?retryWrites=true&w=majority
```

Leave all other values at their defaults for the first run.

---

## Step 2 — Start the Stack

```bash
docker compose up --build
```

First run takes ~60 seconds (pulls LocalStack image, builds Python image, installs dependencies). Subsequent runs start in ~5 seconds.

You should see:
```
localstack-1  | Ready.
api-1         | INFO:     Uvicorn running on http://0.0.0.0:8000
worker-1      | 2026-07-06 [SCHEDULER] Scheduler started | TOTAL_BLOCKS=512 BLOCK_SIZE=16 ...
```

Verify everything is up:
```bash
curl http://localhost:8000/status
```

Expected response:
```json
{
  "request_counts": {"QUEUED":0,"RUNNING":0,"PREEMPTED":0,"SWAPPED":0,"COMPLETED":0,"FAILED":0},
  "memory": {"total_blocks":512,"free_blocks":512,"utilization_pct":0.0}
}
```

---

## Step 3 — Install the Load Generator

In a second terminal (no Docker needed — runs against the API directly):

```bash
pip install requests
```

---

## Step 4 — Fire a Burst of Requests

```bash
python scripts/load_test.py --requests 20 --delay 0.05
```

You'll see each request submitted with its profile (short/medium/long), then a live status poll:

```
Submitting 20 requests to http://localhost:8000 ...

  [01] long   | prompt= 611 max_tokens= 988 priority=1 -> c986df43-...
  [02] medium | prompt= 290 max_tokens= 399 priority=2 -> 19d214ff-...
  ...

Polling until complete ...
  RUNNING= 15  QUEUED=  6  SWAPPED=  0  COMPLETED=  0  FREE_BLOCKS=374  UTIL=26.95%
  RUNNING= 19  QUEUED=  1  SWAPPED=  1  FREE_BLOCKS= 28  UTIL=94.53%
  RUNNING= 19  QUEUED=  1  SWAPPED=  1  FREE_BLOCKS=  9  UTIL=98.24%
  RUNNING= 18  QUEUED=  1  SWAPPED=  2  FREE_BLOCKS= 39  UTIL=92.38%
  ...
```

Watch for:
- **`QUEUED > 0`** — requests waiting not because workers are busy, but because **VRAM is full**
- **`SWAPPED > 0`** — active preemptions; a request's KV state has been evicted to MongoDB
- **`UTIL > 95%`** — the pool is nearly saturated; this is the memory-bound regime

---

## Step 5 — Inspect Individual Requests

Pick a `request_id` from the load generator output and trace its full lifecycle:

```bash
curl http://localhost:8000/requests/c986df43-3a6c-4236-8dbb-5ad79f3ba341
```

You'll see a `history` array like:
```json
"history": [
  {"ts": "...", "event": "SUBMITTED"},
  {"ts": "...", "event": "ADMITTED",   "blocks": 39},
  {"ts": "...", "event": "GREW",        "blocks": 40},
  {"ts": "...", "event": "SWAPPED_OUT", "freed_blocks": 41},
  {"ts": "...", "event": "SWAPPED_IN",  "blocks": 41},
  {"ts": "...", "event": "SWAPPED_OUT", "freed_blocks": 42},
  {"ts": "...", "event": "SWAPPED_IN",  "blocks": 42},
  {"ts": "...", "event": "COMPLETED",   "total_generated": 988}
]
```

This is the request-level audit trail — every block grow, every eviction, every resume — all timestamped.

---

## Step 6 — Change Policy Mid-Run (Optional)

While the load generator is running or between runs, try switching the preemption policy:

```bash
# Switch to footprint-based eviction (evicts the largest request first)
curl -X PATCH http://localhost:8000/admin/config \
  -H "Content-Type: application/json" \
  -d '{"PREEMPTION_POLICY": "footprint"}'

# Switch to recompute mode (discard KV state instead of saving to MongoDB)
curl -X PATCH http://localhost:8000/admin/config \
  -H "Content-Type: application/json" \
  -d '{"RESUME_MODE": "recompute"}'
```

---

## Step 7 — Reset and Repeat

To clear all state for a fresh run:

```bash
curl -X POST http://localhost:8000/admin/reset
```

Then run the load generator again, perhaps with different parameters:

```bash
# Smaller pool → much more aggressive preemption
curl -X PATCH http://localhost:8000/admin/config \
  -H "Content-Type: application/json" \
  -d '{"TOTAL_BLOCKS": 64}'

python scripts/load_test.py --requests 30
```

---

## Step 8 — Tear Down

```bash
docker compose down
```

To also remove the LocalStack volume:
```bash
docker compose down -v
```

---

## Useful Commands Cheatsheet

```bash
# Check container status
docker compose ps

# Stream worker scheduler logs live
docker compose logs worker -f

# Stream API logs live
docker compose logs api -f

# Hit all key endpoints
curl http://localhost:8000/status
curl http://localhost:8000/metrics?last_n=20
curl "http://localhost:8000/requests?status=SWAPPED"
curl http://localhost:8000/admin/config

# Run unit tests
$env:PYTHONPATH="."; pytest tests/ -v
```
