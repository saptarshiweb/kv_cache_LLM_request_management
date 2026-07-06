from enum import Enum
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, timezone
import uuid

class RequestStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PREEMPTED = "PREEMPTED"
    SWAPPED = "SWAPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class InferenceRequest(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    prompt_tokens: int
    max_tokens: int
    generated_tokens: int = 0
    priority: int = 1
    status: RequestStatus = RequestStatus.QUEUED
    allocated_blocks: int = 0
    history: List[dict] = Field(default_factory=list)
    
    def add_event(self, event_type: str, **kwargs):
        self.history.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **kwargs
        })
