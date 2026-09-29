# llm.py cost accounting, no network: python test_llm.py
import os
import llm

os.environ["LLM_PRICE_IN"], os.environ["LLM_PRICE_OUT"] = "0.30", "1.20"
llm.usage.clear()
llm.set_stage("plan")
llm._count(517, 1606)
llm.set_stage("extract")
llm._count(100_000, 20_000)
llm._count(None, None)  # an API that reports no usage still counts the call
c = llm.cost()
assert c["plan"] == {"calls": 1, "in": 517, "out": 1606, "usd": 0.0021}
assert c["extract"]["calls"] == 2 and c["extract"]["usd"] == 0.054  # 0.1M x $0.30 + 0.02M x $1.20
assert c["total"]["calls"] == 3 and c["total"]["usd"] == 0.0561
os.environ["LLM_PRICE_IN"] = os.environ["LLM_PRICE_OUT"] = "0"  # a local model
assert llm.cost()["total"]["usd"] == 0
print("ok")
