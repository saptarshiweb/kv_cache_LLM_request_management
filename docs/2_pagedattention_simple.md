# PagedAttention: Explained Simply

## The Old Way of Storing KV-Cache
When an LLM generates a response, it stores the history of the conversation in memory (the KV-Cache). 
In the past, systems tried to reserve memory in one **big, continuous chunk**. 
Imagine trying to park a limousine in a crowded parking lot. Even if there are 10 empty parking spots scattered around, you can't park the limo because the spots aren't right next to each other. This is called **Memory Fragmentation**.

Because they didn't know exactly how long the AI's answer would be, older systems had to guess and reserve the absolute maximum amount of memory for *every* request. This wasted a massive amount of VRAM (over 50% was often wasted on empty, reserved space).

## What is PagedAttention?
Researchers created a system called **PagedAttention** (which powers engines like vLLM). 
They solved this by breaking the memory up into tiny, fixed-size chunks called **"Blocks"** (or pages). 

Going back to our parking analogy: instead of parking a single limousine, PagedAttention breaks the limo down into 10 separate motorcycles. Now, you can park those motorcycles in any empty spot anywhere in the parking lot. They don't need to be next to each other!

As the AI generates more words, it simply asks for one more "Block" and puts it in any empty space it can find. 
This completely eliminates wasted space. You can fit way more requests into the GPU at the same time.

## How Our Simulator Implements PagedAttention
Our project isn't just a queue; it specifically simulates how PagedAttention works!

In our code, you'll see a component called the `BlockMemoryManager`. 
- We divide our fake "VRAM" into a pool of 512 `TOTAL_BLOCKS`.
- We define `BLOCK_SIZE_TOKENS = 16` (meaning each block holds 16 words).
- When a request comes in, we don't reserve one massive chunk of memory. We just give it the exact number of blocks it needs for the prompt.
- As it generates text (`generated_tokens` goes up), it occasionally asks the pool for 1 more block.
- We track this using a dictionary mapping requests to a count of non-continuous blocks, just like PagedAttention's block tables.

Because of this PagedAttention design, our simulator can dynamically juggle memory, pausing and resuming requests block-by-block, ensuring that we never waste a single byte of simulated GPU memory.
