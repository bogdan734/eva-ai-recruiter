import os, asyncio, httpx
from src.call.script_template import render_system_prompt
API="https://api.vapi.ai"; key=os.environ["VAPI_API_KEY"]; aid=os.environ["VAPI_ASSISTANT_ID"]
prompt=render_system_prompt(candidate_name="невідомий (вхідний дзвінок)",candidate_phone="невідомий",
  candidate_position="невідомо (запитати у кандидата)",candidate_region="невідомо (запитати у кандидата)",source="inbound_call")
body={"model":{"provider":"anthropic","model":"claude-haiku-4-5-20251001","maxTokens":200,"temperature":0.4,
  "messages":[{"role":"system","content":prompt}]}}
async def main():
    async with httpx.AsyncClient(timeout=30) as c:
        r=await c.patch(f"{API}/assistant/{aid}",headers={"Authorization":f"Bearer {key}"},json=body); r.raise_for_status()
        p=r.json()["model"]["messages"][0]["content"]
        print("deployed len:",len(p))
        for m in ["forms.gle","COLD BASE","залиште"]:
            print(f"  {m}: {m in p}")
asyncio.run(main())
