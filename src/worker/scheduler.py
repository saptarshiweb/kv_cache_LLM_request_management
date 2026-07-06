"""
Scheduler Worker — the heart of the KV-cache simulator.

Each tick of the loop does three things (mirroring the PRD pseudocode):
  1. Admit: pull messages from SQS; start any request that fits in free blocks.
  2. Decode-step: advance every RUNNING request by TOKENS_PER_STEP; grow their
     block allocation, preempting if needed.
  3. Resume: try to re-admit SWAPPED requests now that blocks may be free.

All state is persisted to MongoDB so the FastAPI gateway can serve it.
"""

import asyncio
import json
import math
import os
import logging
from datetime import datetime, timezone
from typing import List

from dotenv import load_dotenv

load_dotenv()

from src.core.memory_manager import BlockMemoryManager
from src.core.models import InferenceRequest, RequestStatus
from src.db.mongo import db_client
from src.queue.sqs import sqs_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [SCHEDULER] %(message)s")
log = logging.getLogger(__name__)

# ─── Configuration (all from .env) ─────────────────────────────────────────────
TOTAL_BLOCKS         = int(os.getenv("TOTAL_BLOCKS", 512))
BLOCK_SIZE_TOKENS    = int(os.getenv("BLOCK_SIZE_TOKENS", 16))
TOKENS_PER_STEP      = int(os.getenv("TOKENS_PER_STEP", 8))
TICK_INTERVAL_MS     = int(os.getenv("TICK_INTERVAL_MS", 200))
PREEMPTION_POLICY    = os.getenv("PREEMPTION_POLICY", "priority")   # "priority" | "footprint"
RESUME_MODE          = os.getenv("RESUME_MODE", "swap")             # "swap" | "recompute"
SWAP_IN_LATENCY_MS   = int(os.getenv("SWAP_IN_LATENCY_MS", 300))
RECOMPUTE_PENALTY_FACTOR = float(os.getenv("RECOMPUTE_PENALTY_FACTOR", 0.5))

# ─── Globals (single-writer; safe for single asyncio event loop) ────────────────
memory_manager = BlockMemoryManager(
    total_blocks=TOTAL_BLOCKS,
    block_size_tokens=BLOCK_SIZE_TOKENS,
)


# ─── Helpers ────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def blocks_needed(req: InferenceRequest) -> int:
    seq_len = req.prompt_tokens + req.generated_tokens
    return memory_manager.blocks_needed_for_sequence(seq_len)


async def _persist(req: InferenceRequest):
    col = db_client.get_requests_collection()
    await col.replace_one({"id": req.id}, req.model_dump(), upsert=True)


async def _log_event(request_id: str, event_type: str, detail: dict = None):
    col = db_client.get_events_collection()
    await col.insert_one({
        "ts": now_iso(),
        "request_id": request_id,
        "event_type": event_type,
        "detail": detail or {},
    })


async def _snapshot_metrics(running: List[InferenceRequest], queue_depth: int, swapped_count: int):
    col = db_client.db.get_collection("metrics_snapshots")
    snap = memory_manager.get_snapshot()
    await col.insert_one({
        "ts": now_iso(),
        "queue_depth": queue_depth,
        "running_count": len(running),
        "swapped_count": swapped_count,
        "free_blocks": snap["free_blocks"],
        "total_blocks": snap["total_blocks"],
        "utilization_pct": round(
            100 * (snap["total_blocks"] - snap["free_blocks"]) / snap["total_blocks"], 2
        ),
        "preemption_count": 0,  # updated in preempt logic
    })


async def _fetch_running() -> List[InferenceRequest]:
    col = db_client.get_requests_collection()
    docs = await col.find({"status": RequestStatus.RUNNING}).to_list(length=1000)
    return [InferenceRequest(**{k: v for k, v in d.items() if k != "_id"}) for d in docs]


async def _fetch_swapped() -> List[InferenceRequest]:
    col = db_client.get_requests_collection()
    docs = await col.find({"status": RequestStatus.SWAPPED}).to_list(length=1000)
    return [InferenceRequest(**{k: v for k, v in d.items() if k != "_id"}) for d in docs]


# ─── Preemption ─────────────────────────────────────────────────────────────────

def _pick_victim(running: List[InferenceRequest]) -> InferenceRequest:
    """Return the request to preempt according to the configured policy."""
    if PREEMPTION_POLICY == "footprint":
        # Evict the request using the most blocks (frees the most memory)
        return max(running, key=lambda r: memory_manager.allocated(r.id))
    else:
        # Default: evict the lowest-priority, oldest-first request
        return min(running, key=lambda r: (r.priority, r.submitted_at))


async def _preempt(req: InferenceRequest):
    """Preempt a running request: free its blocks and move to SWAPPED/PREEMPTED."""
    freed_blocks = memory_manager.allocated(req.id)
    memory_manager.free(req.id)

    if RESUME_MODE == "swap":
        req.status = RequestStatus.SWAPPED
        req.add_event("SWAPPED_OUT", freed_blocks=freed_blocks)
        # Persist simulated KV state to MongoDB "swapped_state" collection
        await db_client.db.get_collection("swapped_state").replace_one(
            {"request_id": req.id},
            {
                "request_id": req.id,
                "generated_tokens": req.generated_tokens,
                "block_count": freed_blocks,
                "swapped_at": now_iso(),
            },
            upsert=True,
        )
    else:
        # Recompute mode: discard KV state, pay a recompute penalty on resume
        req.status = RequestStatus.PREEMPTED
        req.add_event("PREEMPTED", freed_blocks=freed_blocks, mode="recompute")

    log.info(f"Preempted {req.id} ({RESUME_MODE} mode), freed {freed_blocks} blocks")
    await _persist(req)
    await _log_event(req.id, req.status, {"freed_blocks": freed_blocks})


# ─── Main Scheduler Loop ─────────────────────────────────────────────────────────

async def scheduler_loop():
    tick_interval = TICK_INTERVAL_MS / 1000.0
    log.info(
        f"Scheduler started | TOTAL_BLOCKS={TOTAL_BLOCKS} BLOCK_SIZE={BLOCK_SIZE_TOKENS} "
        f"TOKENS_PER_STEP={TOKENS_PER_STEP} POLICY={PREEMPTION_POLICY} MODE={RESUME_MODE}"
    )

    while True:
        # ── 1. ADMIT: pull messages from SQS ───────────────────────────────────
        messages = sqs_client.poll_messages(max_messages=10, wait_seconds=1)
        for msg in messages:
            try:
                body = json.loads(msg["Body"])
                req = InferenceRequest(**body)
                needed = blocks_needed(req)

                if memory_manager.can_fit(needed):
                    memory_manager.allocate(req.id, needed)
                    req.status = RequestStatus.RUNNING
                    req.allocated_blocks = needed
                    req.add_event("ADMITTED", blocks=needed)
                    await _persist(req)
                    await _log_event(req.id, "ADMITTED", {"blocks": needed})
                    sqs_client.delete_message(msg["ReceiptHandle"])
                    log.info(f"Admitted {req.id} | needed={needed} free_after={memory_manager.free_blocks}")
                else:
                    # Leave message in queue; SQS visibility timeout will re-expose it
                    log.debug(f"Deferred {req.id} | needed={needed} free={memory_manager.free_blocks}")
            except Exception as e:
                log.error(f"Error processing SQS message: {e}")

        # ── 2. DECODE STEP: advance all running requests ───────────────────────
        running = await _fetch_running()
        for req in running:
            req.generated_tokens += TOKENS_PER_STEP
            needed_now = blocks_needed(req)
            currently_allocated = memory_manager.allocated(req.id)
            extra = needed_now - currently_allocated

            if extra > 0:
                if memory_manager.can_fit(extra):
                    memory_manager.grow(req.id, extra)
                    req.allocated_blocks = needed_now
                    req.add_event("GREW", blocks=needed_now)
                    await _log_event(req.id, "GREW", {"blocks": needed_now})
                else:
                    # Memory pressure — preempt a victim (may be this req or another)
                    all_running = await _fetch_running()
                    victim = _pick_victim(all_running)
                    await _preempt(victim)
                    # If the victim was not us, try growing again
                    if victim.id != req.id and memory_manager.can_fit(extra):
                        memory_manager.grow(req.id, extra)
                        req.allocated_blocks = needed_now
                        req.add_event("GREW", blocks=needed_now)
                    else:
                        # We were preempted; skip further processing for this req
                        continue

            # Check if request is done
            if req.generated_tokens >= req.max_tokens:
                memory_manager.free(req.id)
                req.status = RequestStatus.COMPLETED
                req.allocated_blocks = 0
                req.add_event("COMPLETED", total_generated=req.generated_tokens)
                log.info(f"Completed {req.id} | generated={req.generated_tokens} tokens")
                await _log_event(req.id, "COMPLETED", {"generated_tokens": req.generated_tokens})

            await _persist(req)

        # ── 3. RESUME: try to re-admit SWAPPED requests ────────────────────────
        swapped = await _fetch_swapped()
        # Sort by priority descending (highest priority resumes first)
        swapped.sort(key=lambda r: (-r.priority, r.submitted_at))
        for req in swapped:
            needed = blocks_needed(req)
            if memory_manager.can_fit(needed):
                if RESUME_MODE == "swap":
                    # Simulate swap-in latency
                    await asyncio.sleep(SWAP_IN_LATENCY_MS / 1000.0)
                    req.add_event("SWAPPED_IN", blocks=needed)
                else:
                    # Recompute: pay a fraction of the original prefill cost
                    penalty = (req.prompt_tokens / TOKENS_PER_STEP) * tick_interval * RECOMPUTE_PENALTY_FACTOR
                    await asyncio.sleep(penalty)
                    req.add_event("RECOMPUTED", blocks=needed)

                memory_manager.allocate(req.id, needed)
                req.status = RequestStatus.RUNNING
                req.allocated_blocks = needed
                await _persist(req)
                await _log_event(req.id, "RESUMED", {"blocks": needed, "mode": RESUME_MODE})
                log.info(f"Resumed {req.id} | blocks={needed} mode={RESUME_MODE}")

        # ── 4. METRICS SNAPSHOT ────────────────────────────────────────────────
        await _snapshot_metrics(running, len(messages), len(swapped))

        await asyncio.sleep(tick_interval)


async def main():
    await db_client.connect()
    sqs_client.init_queue()
    await scheduler_loop()


if __name__ == "__main__":
    asyncio.run(main())
