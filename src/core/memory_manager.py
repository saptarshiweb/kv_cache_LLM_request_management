import math
from typing import Dict

class BlockMemoryManager:
    def __init__(self, total_blocks: int = 512, block_size_tokens: int = 16):
        self.total_blocks = total_blocks
        self.block_size_tokens = block_size_tokens
        self.free_blocks = total_blocks
        # mapping of request_id -> number of blocks allocated
        self.allocated_by_request: Dict[str, int] = {}
        
    def can_fit(self, num_blocks: int) -> bool:
        return self.free_blocks >= num_blocks
        
    def allocate(self, request_id: str, num_blocks: int) -> bool:
        if not self.can_fit(num_blocks):
            return False
        
        self.free_blocks -= num_blocks
        if request_id not in self.allocated_by_request:
            self.allocated_by_request[request_id] = 0
        self.allocated_by_request[request_id] += num_blocks
        return True
        
    def free(self, request_id: str):
        if request_id in self.allocated_by_request:
            blocks = self.allocated_by_request.pop(request_id)
            self.free_blocks += blocks
            
    def grow(self, request_id: str, extra_blocks: int) -> bool:
        return self.allocate(request_id, extra_blocks)
        
    def blocks_needed_for_sequence(self, sequence_length: int) -> int:
        return math.ceil(sequence_length / self.block_size_tokens)
        
    def allocated(self, request_id: str) -> int:
        return self.allocated_by_request.get(request_id, 0)
        
    def get_snapshot(self) -> dict:
        return {
            "total_blocks": self.total_blocks,
            "free_blocks": self.free_blocks,
            "allocated_by_request": self.allocated_by_request.copy()
        }
