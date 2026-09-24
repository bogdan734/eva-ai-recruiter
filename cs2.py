import asyncio, json
from src.scraper.cold_sourcing import run_cold_sourcing_cycle
print(json.dumps(asyncio.run(run_cold_sourcing_cycle()), ensure_ascii=False, indent=2))
