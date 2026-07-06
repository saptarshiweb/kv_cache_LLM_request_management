import pytest
from src.core.memory_manager import BlockMemoryManager

def test_initialization():
    manager = BlockMemoryManager(total_blocks=100, block_size_tokens=16)
    assert manager.free_blocks == 100
    assert manager.total_blocks == 100
    assert manager.block_size_tokens == 16
    assert manager.allocated("any") == 0

def test_allocation():
    manager = BlockMemoryManager(total_blocks=10)
    
    # Successful allocation
    assert manager.allocate("req1", 4) is True
    assert manager.free_blocks == 6
    assert manager.allocated("req1") == 4
    
    # Cannot allocate more than available
    assert manager.allocate("req2", 7) is False
    assert manager.free_blocks == 6
    assert manager.allocated("req2") == 0

def test_free():
    manager = BlockMemoryManager(total_blocks=10)
    manager.allocate("req1", 5)
    manager.allocate("req2", 3)
    
    manager.free("req1")
    assert manager.free_blocks == 7
    assert manager.allocated("req1") == 0
    assert manager.allocated("req2") == 3

def test_grow():
    manager = BlockMemoryManager(total_blocks=10)
    manager.allocate("req1", 2)
    
    assert manager.grow("req1", 3) is True
    assert manager.free_blocks == 5
    assert manager.allocated("req1") == 5
    
    assert manager.grow("req1", 6) is False
    assert manager.free_blocks == 5
    assert manager.allocated("req1") == 5

def test_blocks_needed_for_sequence():
    manager = BlockMemoryManager(block_size_tokens=16)
    
    assert manager.blocks_needed_for_sequence(0) == 0
    assert manager.blocks_needed_for_sequence(1) == 1
    assert manager.blocks_needed_for_sequence(16) == 1
    assert manager.blocks_needed_for_sequence(17) == 2
    assert manager.blocks_needed_for_sequence(32) == 2
    assert manager.blocks_needed_for_sequence(33) == 3
