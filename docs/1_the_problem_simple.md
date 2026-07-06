# The Problem & Solution: Explained Simply

## The Old Way: Web Servers (Compute-Bound)
Imagine a restaurant kitchen (a traditional web server). You have 4 chefs (CPU cores or threads). If 4 orders (requests) come in, the chefs start cooking. If a 5th order comes in, it has to wait in line until a chef is free. 
In traditional software, we schedule tasks based on **Compute (CPU/Workers)**. If you have free workers, you let requests in. If you don't, you queue them.

## The LLM Way: VRAM is the Bottleneck (Memory-Bound)
Now imagine LLM Inference (like ChatGPT). In this kitchen, it's not the chefs that run out, it's the **counter space**. 
Generating text with an LLM requires saving the "context" of the conversation in the GPU's memory. This memory is called the **KV-Cache**. Every single word the AI generates takes up a little bit more physical space on the GPU.

So, a GPU might be only using 20% of its processing power (the chefs are bored!), but its memory (the counter space) is 100% full. If you try to admit a new request just because the GPU processor looks "idle", the system will crash with an "Out Of Memory" (OOM) error.

## The Core Problem
Standard web server tools don't understand this. They only look at the chefs, not the counter space. If we put a normal web server in front of an LLM, it will let too many requests in and crash the GPU because they assume memory is limitless and processing power is the only limit.

## Our Solution
We built a **Memory-Aware Request Queue**. 
Instead of looking at CPU usage, our queue acts as a bouncer at the door, constantly looking at the GPU's **VRAM (KV-Cache)**. 

Here is what our simulator does:
1. **Admission:** Before letting a request in, it checks: "Do we have enough memory blocks left for this prompt?" If no, the request stays in the queue, even if the CPU is doing nothing.
2. **Growth:** As the AI types out the answer, our system "allocates" more memory blocks in real-time.
3. **Preemption (The Bouncer):** What happens if the memory fills up while generating? Our system picks a lower-priority request, kicks it out of the active memory (saves its state to a backup), and gives its memory to the more important request.
4. **Resumption:** When memory frees up later, it brings the paused request back and finishes it.

By building this, we prevent the GPU from ever crashing, and we maximize how many requests we can handle at the exact same time without running out of memory!
