"""Entrypoint so the worker can be run as `python -m src.worker.scheduler`"""
import asyncio
from src.worker.scheduler import main

asyncio.run(main())
