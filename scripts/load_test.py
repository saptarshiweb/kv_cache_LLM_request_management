"""
Load Generator — Phase 4 demo script.

Fires a burst of simulated inference requests with varied sizes to:
  - Saturate the KV-cache block pool
  - Trigger visible preemptions
  - Demonstrate memory-bound vs compute-bound scheduling

Usage:
    python scripts/load_test.py --url http://localhost:8000 --requests 30
"""
import argparse
import random
import time
import requests as http

def submit(api_url: str, prompt_tokens: int, max_tokens: int, priority: int) -> dict:
    resp = http.post(f"{api_url}/requests", json={
        "prompt_tokens": prompt_tokens,
        "max_tokens": max_tokens,
        "priority": priority,
    })
    resp.raise_for_status()
    return resp.json()

def main():
    parser = argparse.ArgumentParser(description="KV-Cache Simulator Load Generator")
    parser.add_argument("--url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--requests", type=int, default=30, help="Number of requests to submit")
    parser.add_argument("--delay", type=float, default=0.1, help="Seconds between submissions")
    args = parser.parse_args()

    print(f"Submitting {args.requests} requests to {args.url} ...\n")

    submitted = []
    for i in range(args.requests):
        # Mix of short, medium, and long requests to create varied memory pressure
        profile = random.choice(["short", "medium", "long"])
        if profile == "short":
            prompt, max_tok, pri = random.randint(32, 128), random.randint(64, 128), 3
        elif profile == "medium":
            prompt, max_tok, pri = random.randint(128, 512), random.randint(128, 512), 2
        else:
            prompt, max_tok, pri = random.randint(512, 1024), random.randint(512, 1024), 1

        result = submit(args.url, prompt, max_tok, pri)
        submitted.append(result["request_id"])
        print(f"  [{i+1:02d}] {profile:6s} | prompt={prompt:4d} max_tokens={max_tok:4d} priority={pri} -> {result['request_id']}")
        time.sleep(args.delay)

    print(f"\nSubmitted {len(submitted)} requests.")
    print(f"Watch status: {args.url}/status")
    print(f"Watch metrics: {args.url}/metrics")

    # Poll status until all are done or 120s elapses
    print("\nPolling until complete (Ctrl-C to stop) ...")
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            status = http.get(f"{args.url}/status").json()
            counts = status.get("request_counts", {})
            mem = status.get("memory", {})
            print(
                f"  RUNNING={counts.get('RUNNING',0):3d}  "
                f"QUEUED={counts.get('QUEUED',0):3d}  "
                f"SWAPPED={counts.get('SWAPPED',0):3d}  "
                f"COMPLETED={counts.get('COMPLETED',0):3d}  "
                f"FREE_BLOCKS={mem.get('free_blocks','?')}  "
                f"UTIL={mem.get('utilization_pct','?')}%"
            )
            if counts.get("RUNNING", 0) == 0 and counts.get("QUEUED", 0) == 0 and counts.get("SWAPPED", 0) == 0:
                print("\nAll requests completed!")
                break
        except Exception as e:
            print(f"  Status check failed: {e}")
        time.sleep(2)

if __name__ == "__main__":
    main()
