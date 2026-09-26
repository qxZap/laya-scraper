"""Spot-check extraction quality: the LLM compares each extracted value with the page itself.

    python grade.py runs/govuk.json [--n 10]
Per field: correct | wrong | missed (on the page but left empty) | absent (empty, and not on the page).
Precision = correct / (correct + wrong), recall = correct / (correct + wrong + missed). A proxy, not ground truth.
"""
import argparse, json, os, random, sys
from collections import Counter

from extract import GRADE_PROMPT as PROMPT, outline, page_tree
from harvest import slug
from llm import chat_json

def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()
    run = json.load(open(a.run, encoding="utf-8"))
    cache = json.load(open(os.path.join("cache", slug(run["site"]) + "--" + slug(run["prompt"]) + ".json"), encoding="utf-8"))
    plan, fields = run["plan"], run["plan"]["fields"]
    items = random.Random(0).sample(run["items"], min(a.n, len(run["items"])))
    tally = {f["name"]: Counter() for f in fields}
    for x in items:
        tree = page_tree(cache["pages"][x["url"]]["html"], x["url"], cache["info"].get("item_pattern", ""))
        record = "\n".join(f"{f['name']}: {json.dumps(x.get(f['name'], ''), ensure_ascii=False)[:300]}" for f in fields)
        try:
            verdict = chat_json(PROMPT.format(what=plan.get("singular") or plan["target"], url=x["url"], record=record,
                                              page="\n".join(outline(tree, cap=220)[1]),
                                              fields="\n".join(f"- {f['name']}: {f['description']}" for f in fields)))
        except Exception as e:
            print(f"  ! {x['url']}: {e}", file=sys.stderr)
            continue
        for f in fields:
            tally[f["name"]][str(verdict.get(f["name"], "?")).lower()] += 1
    tot = sum(tally.values(), Counter())
    for name, c in list(tally.items()) + [("ALL FIELDS", tot)]:
        found = c["correct"] + c["wrong"]
        prec = f"{c['correct'] / found:.0%}" if found else "-"
        rec = f"{c['correct'] / (found + c['missed']):.0%}" if found + c["missed"] else "-"
        print(f"  {name:20} correct {c['correct']:3}  wrong {c['wrong']:3}  missed {c['missed']:3}  absent {c['absent']:3}"
              f"   precision {prec:>4}  recall {rec:>4}")


if __name__ == "__main__":
    main()
