"""
FastAPI Control Plane for the KV-Cache Simulator.

Endpoints:
  POST /requests               — Submit a new simulated inference request
  GET  /requests               — List requests, filterable by status
  GET  /requests/{id}          — Get a single request and its history
  GET  /status                 — Scheduler snapshot (free blocks, counts)
  GET  /metrics                — Time-series metrics from metrics_snapshots
  GET  /admin/config           — View current simulation config
  PATCH /admin/config          — Update simulation config at runtime
  POST /admin/reset            — Reset all state (Mongo + SQS)
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

from src.db.mongo import db_client
from src.queue.sqs import sqs_client
from src.core.models import InferenceRequest, RequestStatus


# ─── App lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db_client.connect()
    sqs_client.init_queue()
    yield
    await db_client.disconnect()

app = FastAPI(
    title="KV-Cache Memory-Aware Request Queue",
    description="Simulates vLLM-style KV-cache block-based scheduling",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Request Submission ─────────────────────────────────────────────────────────

class SubmitParams(BaseModel):
    prompt_tokens: int
    max_tokens: int
    priority: int = 1


@app.post("/requests", status_code=201, tags=["Requests"])
async def submit_request(params: SubmitParams):
    """Submit a simulated inference request. It is pushed to SQS and returned to the client."""
    req = InferenceRequest(
        prompt_tokens=params.prompt_tokens,
        max_tokens=params.max_tokens,
        priority=params.priority,
    )
    req.add_event("SUBMITTED")

    # Persist immediately so status is queryable even before admission
    col = db_client.get_requests_collection()
    await col.insert_one(req.model_dump())

    # Push to SQS for the scheduler to pick up
    sqs_client.send_message(req.model_dump(mode="json"))

    return {"request_id": req.id, "status": req.status}


# ─── Request Query ──────────────────────────────────────────────────────────────

@app.get("/requests", tags=["Requests"])
async def list_requests(status: Optional[RequestStatus] = Query(None)):
    """List all requests, optionally filtered by status."""
    col = db_client.get_requests_collection()
    query = {}
    if status:
        query["status"] = status
    docs = await col.find(query).sort("submitted_at", -1).to_list(length=500)
    for d in docs:
        d.pop("_id", None)
    return docs


@app.get("/requests/{request_id}", tags=["Requests"])
async def get_request(request_id: str):
    """Get the current state and full history of a single request."""
    col = db_client.get_requests_collection()
    doc = await col.find_one({"id": request_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Request not found")
    doc.pop("_id", None)
    return doc


# ─── Status Snapshot ────────────────────────────────────────────────────────────

@app.get("/status", tags=["Observability"])
async def get_status():
    """Current scheduler snapshot: block pool usage and request counts by state."""
    col = db_client.get_requests_collection()

    counts = {}
    for s in RequestStatus:
        counts[s.value] = await col.count_documents({"status": s.value})

    # Latest metrics snapshot for block info
    snap_col = db_client.db.get_collection("metrics_snapshots")
    latest = await snap_col.find_one(sort=[("ts", -1)])
    if latest:
        latest.pop("_id", None)
    else:
        latest = {"free_blocks": "N/A", "total_blocks": "N/A", "utilization_pct": "N/A"}

    return {
        "request_counts": counts,
        "memory": {
            "total_blocks": latest.get("total_blocks"),
            "free_blocks": latest.get("free_blocks"),
            "utilization_pct": latest.get("utilization_pct"),
        },
        "ts": datetime.now(timezone.utc).isoformat(),
    }


# ─── Metrics ────────────────────────────────────────────────────────────────────

@app.get("/metrics", tags=["Observability"])
async def get_metrics(last_n: int = Query(100, description="Number of most recent snapshots to return")):
    """Return time-series metrics snapshots for charting."""
    col = db_client.db.get_collection("metrics_snapshots")
    docs = await col.find().sort("ts", -1).limit(last_n).to_list(length=last_n)
    for d in docs:
        d.pop("_id", None)
    docs.reverse()  # chronological order for charts
    return {"count": len(docs), "snapshots": docs}


# ─── Admin: Config ──────────────────────────────────────────────────────────────

# Mutable runtime config (starts from env; scheduler reads from here via shared state)
_runtime_config = {
    "TOTAL_BLOCKS":              int(os.getenv("TOTAL_BLOCKS", 512)),
    "BLOCK_SIZE_TOKENS":         int(os.getenv("BLOCK_SIZE_TOKENS", 16)),
    "TOKENS_PER_STEP":           int(os.getenv("TOKENS_PER_STEP", 8)),
    "TICK_INTERVAL_MS":          int(os.getenv("TICK_INTERVAL_MS", 200)),
    "PREEMPTION_POLICY":         os.getenv("PREEMPTION_POLICY", "priority"),
    "RESUME_MODE":               os.getenv("RESUME_MODE", "swap"),
    "SWAP_IN_LATENCY_MS":        int(os.getenv("SWAP_IN_LATENCY_MS", 300)),
    "RECOMPUTE_PENALTY_FACTOR":  float(os.getenv("RECOMPUTE_PENALTY_FACTOR", 0.5)),
}


@app.get("/admin/config", tags=["Admin"])
async def get_config():
    """Return the current runtime simulation configuration."""
    return _runtime_config


class ConfigPatch(BaseModel):
    TOTAL_BLOCKS: Optional[int] = None
    BLOCK_SIZE_TOKENS: Optional[int] = None
    TOKENS_PER_STEP: Optional[int] = None
    TICK_INTERVAL_MS: Optional[int] = None
    PREEMPTION_POLICY: Optional[str] = None
    RESUME_MODE: Optional[str] = None
    SWAP_IN_LATENCY_MS: Optional[int] = None
    RECOMPUTE_PENALTY_FACTOR: Optional[float] = None


@app.patch("/admin/config", tags=["Admin"])
async def patch_config(patch: ConfigPatch):
    """Update one or more simulation parameters at runtime."""
    updates = patch.model_dump(exclude_none=True)
    if "PREEMPTION_POLICY" in updates and updates["PREEMPTION_POLICY"] not in ("priority", "footprint"):
        raise HTTPException(status_code=400, detail="PREEMPTION_POLICY must be 'priority' or 'footprint'")
    if "RESUME_MODE" in updates and updates["RESUME_MODE"] not in ("swap", "recompute"):
        raise HTTPException(status_code=400, detail="RESUME_MODE must be 'swap' or 'recompute'")
    _runtime_config.update(updates)
    return {"updated": updates, "config": _runtime_config}


# ─── Admin: Reset ───────────────────────────────────────────────────────────────

@app.post("/admin/reset", tags=["Admin"])
async def reset_simulation():
    """Hard reset: clears all MongoDB collections and purges the SQS queue."""
    db = db_client.db
    for collection_name in ["requests", "events", "block_ledger", "metrics_snapshots", "swapped_state"]:
        await db.get_collection(collection_name).delete_many({})

    sqs_client.purge_queue()

    return {"status": "reset complete", "ts": datetime.now(timezone.utc).isoformat()}
