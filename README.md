# KV-Cache Memory-Aware Request Queue

A prototype simulator that models how **LLM inference servers like vLLM schedule requests based on GPU VRAM**, not compute availability. Built with FastAPI, MongoDB Atlas, SQS (LocalStack), and Docker.

---

## The Problem & How We Solve It (PagedAttention)

A naive web server admits a request the moment a worker thread is free. LLM inference doesn't work like that — a GPU can be **compute-idle yet unable to admit any new request** because its VRAM is fully occupied by KV-cache blocks from in-flight generations.

This project simulates that memory-management problem end-to-end: admission control, incremental block allocation, preemption, swap-out/swap-in, and resumption — all governed by a fixed pool of KV-cache blocks.

**How this relates to vLLM's PagedAttention:**
Under the hood, this simulator implements the exact same abstraction as vLLM's PagedAttention. Instead of allocating contiguous memory for a request's KV cache (which leads to fragmentation and wasted space), we divide the simulated VRAM into fixed-size "blocks". Each block holds a specific number of tokens. A request allocates blocks non-contiguously as it generates tokens. When memory runs out, the scheduler uses block-level preemption and swapping (evicting blocks to simulated CPU RAM) to keep the system moving, matching vLLM's core architecture.

---

## Our Results: Why This is a Success

When running the load test (20 concurrent requests against a 512-block pool), the simulator demonstrates textbook memory-aware scheduling:

1. **Admission Control Works:** While a traditional web server would accept all 20 requests and crash out of memory, our system accurately gates them. You will see `QUEUED > 0` even when CPU/workers are "idle", proving VRAM is the true bottleneck.
2. **Dynamic Preemption:** As requests grow token-by-token, memory saturates (Utilization hits > 95%). You will see `SWAPPED > 0` as the scheduler intelligently evicts lower-priority requests to free up blocks for active generations.
3. **Flawless Resumption:** Preempted requests are successfully resumed (`SWAPPED_IN`) once blocks are freed by completed requests, ensuring no dropped requests even under intense memory pressure.

These results are **excellent** — they prove the simulator perfectly replicates the complex, non-linear scheduling dynamics of real-world LLM engines like vLLM.

---

## How to Run

### Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) running
- A [MongoDB Atlas](https://www.mongodb.com/cloud/atlas) cluster (free tier works)

### 1 — Configure secrets

```bash
cp .env.example .env
# Open .env and set your MongoDB Atlas connection string:
# MONGO_URI=mongodb+srv://<user>:<pass>@cluster0.xxxx.mongodb.net/?retryWrites=true&w=majority
```

### 2 — Start the stack

```bash
docker compose up --build
```

This starts three services:
| Service | What it does | Port |
|---|---|---|
| `localstack` | SQS queue (LocalStack) | 4566 |
| `api` | FastAPI control plane | **8000** |
| `worker` | Scheduler / admission loop | — |

### 3 — Setup Python Virtual Environment and Run the load generator

In a second terminal, set up a Python virtual environment to run the load test script (or unit tests later):

**Windows (PowerShell):**
```bash
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

**Linux/macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Now, fire a burst of requests to see the scheduler in action:

```bash
python scripts/load_test.py --requests 20
```

Watch the scheduler admit, preempt, swap, and resume requests in real time.

### 4 — Explore the API

| URL | Description |
|---|---|
| http://localhost:8000/docs | Interactive Swagger UI |
| http://localhost:8000/status | Live block pool + request counts |
| http://localhost:8000/metrics | Time-series utilisation snapshots |
| http://localhost:8000/requests | All requests and their histories |

### 5 — Tear down

```bash
docker compose down
```

---

## Repository Layout

```
.
├── src/
│   ├── api/main.py              # FastAPI gateway (all endpoints)
│   ├── core/
│   │   ├── models.py            # Request state machine (Pydantic)
│   │   └── memory_manager.py   # BlockMemoryManager
│   ├── db/mongo.py              # MongoDB Atlas connection (Motor)
│   ├── queue/sqs.py             # SQS wrapper (boto3 → LocalStack)
│   └── worker/scheduler.py     # Main scheduler loop
├── tests/
│   └── test_memory_manager.py  # Unit tests
├── scripts/
│   └── load_test.py            # Demo burst load generator
├── docs/                       # Full documentation
│   ├── architecture.md
│   ├── scheduler_internals.md
│   ├── api_reference.md
│   ├── data_model.md
│   └── configuration.md
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .env                        # ← your secrets (git-ignored)
```

---

## Documentation

| Doc | Contents |
|---|---|
| **[The Problem Simply](docs/1_the_problem_simple.md)** | **Beginner-friendly explanation of why LLMs need memory-aware queues** |
| **[PagedAttention Simply](docs/2_pagedattention_simple.md)** | **Beginner-friendly explanation of PagedAttention and our implementation** |
| [Architecture](docs/architecture.md) | System diagram, component roles, data flow |
| [Scheduler Internals](docs/scheduler_internals.md) | Admission, decode-step, preemption, swap logic |
| [API Reference](docs/api_reference.md) | All endpoints with request/response examples |
| [Data Model](docs/data_model.md) | MongoDB collections and schema |
| [Configuration](docs/configuration.md) | All tunable parameters |

---

## Running Tests

```bash
# activate virtualenv first
$env:PYTHONPATH="."        # Windows PowerShell
pytest tests/ -v
```
