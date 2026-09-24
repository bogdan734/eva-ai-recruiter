import asyncio

from src.scheduler.dispatcher import run_slot


asyncio.run(run_slot())
print("RUN_SLOT_DONE")
