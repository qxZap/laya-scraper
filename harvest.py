"""One prompt, any site: find the listing, page through it, design a schema, extract every item.

    python harvest.py https://odi.org/en/ "publications"
    python harvest.py https://www.ukri.org/ "open funding opportunities" --max-items 100 --out grants.csv

  0. plan     the LLM turns the prompt into a target, examples and a field schema  (plans/<slug>.json, reused)
  1. locate   scrape.py crawls for the listing; the LLM picks among the top candidates
  2. paginate list.py walks the listing (next links, load-more, view-all, probing) up to --max-items
  3. extract  laya walks each page's DOM per schema field, learns selectors, verifies every value
The LLM makes a handful of calls per run; everything per-page is laya on the GPU.
"""
import argparse, csv, json, os, re, sys, time

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from extract import Extractor, LLMUnavailable
from list import Fetcher, item_group, walk
import llm
from llm import chat_json
from scrape import Brain, crawl, fetch_requests, norm, parse

PLAN_PROMPT = """A user wants to scrape items from a website. Their request: "{prompt}"

Clarify it into a scraping plan. Return JSON with exactly these keys:
- "target": plural noun phrase for the items, the way a website would label the listing (e.g. "policy papers")
- "singular": the same, singular
- "description": one sentence saying what counts as such an item and what does not
- "listing_names": 5-8 labels websites commonly give the page that lists these items
- "examples": 3 realistic example item titles
- "fields": 5-9 fields to extract from each item's own page. Each: {{"name": snake_case,
  "type": one of text|date|url|people|list|number|money, "description": what it is and how it usually looks
  on the page, "example": a realistic value}}. The first field is the item's title or name. Include the most
  important date(s) and, if relevant, a link to the main document or file. Only fields that commonly appear
  on the item's page. Add "pattern" (a lenient Python regex any valid value must contain) only for fields with a
  well-known format, e.g. DOI, ISBN, patent or grant numbers, amounts of money; omit it for free text."""

JUDGE_PROMPT = """We are looking for the page on {site} that lists the site's {target} ({description}).
Rules, in order:
1. Its listed items must themselves be {target} (look at the sample items), not categories, companies,
   people or topics that merely lead to them.
2. Prefer the main, complete listing (an index or archive with many items, ideally paginated) over a narrow
   sub-collection, a single item, or a mixed homepage.
3. Some sites are directories with no complete listing, only lists by category, company or date. Then pick
   the candidate that directly lists the most {target}, preferring paginated and most recent.
Candidates found by the crawler:

{cands}

Links on the start page (label -> URL), in case the crawler missed the main listing:
{menu}

Return JSON: {{"pick": <candidate number>, "why": "<one short sentence>"}}, or if a start-page link is clearly
the main listing and no candidate is, {{"url": "<that exact URL>", "why": "..."}}."""


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def make_plan(prompt, replan):
    path = os.path.join("plans", slug(prompt) + ".json")
    if os.path.exists(path) and not replan:
        print(f"plan: reusing {path}", file=sys.stderr)
        return json.load(open(path, encoding="utf-8"))
    t = time.time()
    plan = chat_json(PLAN_PROMPT.format(prompt=prompt))
    plan["prompt"] = prompt
    os.makedirs("plans", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    print(f"plan: LLM wrote {path} in {time.time() - t:.1f}s", file=sys.stderr)
    return plan


def guessed_listings(site, plan, known):
    """Listings the crawler can't reach by links (e.g. behind a search box): guess /datasets, /publications, ...
    from the plan's listing names, keep the guesses that answer with a real list."""
    root = "{0.scheme}://{0.netloc}".format(urlsplit(site))
    words = plan["listing_names"] + [plan["target"], plan.get("singular", "")]
    guesses = {f"{root}/{slug(w)}" for w in words if w and len(slug(w)) < 30} - {u.rstrip("/") for u in known}
    found = []
    with ThreadPoolExecutor(8) as pool:
        for r in pool.map(fetch_requests, sorted(guesses)):
            if "html" in r and r["status"] < 400 and norm(r["final_url"]).rstrip("/") not in {u.rstrip("/") for u in known}:
                p = parse(r["final_url"], r["html"])
                _, items = item_group(p)
                titled = [t for t in items.values() if len(t) > 15]
                if len(titled) >= 5:
                    found.append({"url": r["final_url"], "title": p["title"], "items": len(titled), "score": "guessed",
                                  "sample": titled[:6]})
    if found:
        print(f"locate: guessed listings {[f['url'] for f in found]}", file=sys.stderr)
    return found


def locate(site, plan, brain, a):
    target = f"{plan['target']} (also called {', '.join(plan['listing_names'][:4])})"
    res = crawl(site, target, a.max_pages, 3, "auto", a.tabs, brain=brain)
    # the start page may itself be the listing (a search landing page); the judge decides
    top = sorted(res["places"], key=lambda r: -r["score"])[:6]
    top += guessed_listings(site, plan, {p["url"] for p in res["places"]})
    if len(top) < 2:
        return top[0]["url"] if top else res["answer"]
    cands = "\n".join(f"{i}. {p['url']}\n   title: {p['title']}\n   listed items: {p['items']}, score {p['score']}\n"
                      f"   sample items: {' | '.join(p['sample'][:5]) or '(none seen)'}" for i, p in enumerate(top, 1))
    start = fetch_requests(site)
    links = parse(start["final_url"], start["html"])["links"] if "html" in start else {}
    menu = {u: t for u, t in links.items() if 1 < len(t) <= 40}  # menu-like: short labels
    try:
        pick = chat_json(JUDGE_PROMPT.format(site=site, target=plan["target"], description=plan["description"], cands=cands,
                                             menu="\n".join(f"{t} -> {u}" for u, t in list(menu.items())[:80]) or "(none)"))
        if pick.get("url") in menu:
            print(f"locate: LLM chose start-page link {pick['url']} ({pick.get('why', '')})", file=sys.stderr)
            return pick["url"]
        chosen = top[int(pick["pick"]) - 1]["url"]
        print(f"locate: LLM picked #{pick['pick']} {chosen} ({pick.get('why', '')})", file=sys.stderr)
        return chosen
    except Exception as e:  # the crawler's own ranking is a fine fallback
        print(f"locate: judge failed ({e}); using crawler's pick", file=sys.stderr)
        return res["answer"]


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("prompt", help='what to collect, in plain words: "policy documents", "open grants", ...')
    ap.add_argument("--hub", help="skip locating: this URL is the listing")
    ap.add_argument("--max-items", type=int, default=100)
    ap.add_argument("--max-pages", type=int, default=30, help="crawl budget for locating")
    ap.add_argument("--tabs", type=int, default=4)
    ap.add_argument("--replan", action="store_true", help="ask the LLM for a fresh plan instead of reusing one")
    ap.add_argument("--reuse-pages", action="store_true", help="skip locate/paging/fetching; re-extract cached pages")
    ap.add_argument("--guess", action="store_true", help="fill fields that failed validation with laya's DOM walk")
    ap.add_argument("--revalidate", action="store_true", help="ignore saved selectors for this site; map and validate again")
    ap.add_argument("--out", help=".csv or .json")
    a = ap.parse_args()
    t0 = time.time()
    log = lambda *m: print(*m, file=sys.stderr)

    plan = make_plan(a.prompt, a.replan)
    log(f"plan: {plan['target']} — {plan['description']}")
    log(f"      e.g. {' | '.join(plan['examples'][:3])}")
    log(f"      fields: {', '.join(f['name'] + ':' + f['type'] for f in plan['fields'])}")
    brain = Brain(plan["target"])

    cache = os.path.join("cache", slug(a.site) + "--" + slug(a.prompt) + ".json")
    if a.reuse_pages and os.path.exists(cache):  # re-extract only: same listing, same pages
        c = json.load(open(cache, encoding="utf-8"))
        hub, listed, info, pages = c["hub"], c["listed"], c["info"], c["pages"]
        log(f"reusing {len(pages)} cached pages from {cache}")
    else:
        hub = a.hub or locate(a.site, plan, brain, a)
        if not hub:
            raise SystemExit(f"no listing found on {a.site} (blocked, or nothing matching '{plan['target']}'); try --hub URL")
        log(f"listing: {hub}  [{time.time() - t0:.0f}s]")
        f = Fetcher(a.tabs)
        try:
            listed, info = walk(f, hub, a.max_items)
            log(f"paging: {info['mode']}, {len(listed)} items  [{time.time() - t0:.0f}s]")
            pages = {u: {"html": p["html"], "via": p["via"]} for u, p in f.get(list(listed)).items()}
        finally:
            f.close()
        log(f"fetched {len(pages)} item pages  [{time.time() - t0:.0f}s]")
        os.makedirs("cache", exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump({"hub": hub, "listed": listed, "info": info, "pages": pages}, fh)

    t1 = time.time()
    # validated selectors are saved per site + prompt: repeat runs need no LLM and give the same answers
    sel_path = os.path.join("selectors", slug(a.site) + "--" + slug(a.prompt) + ".json")
    names = [f["name"] for f in plan["fields"]]
    known = None
    if os.path.exists(sel_path) and not a.revalidate:
        known = json.load(open(sel_path, encoding="utf-8"))
        if known.get("fields_in_plan") != names:  # the plan changed since: learn again
            known = None
    try:
        got, report = Extractor(brain, plan).run({u: p["html"] for u, p in pages.items()}, info.get("item_pattern", ""),
                                                 guess=a.guess, known=known, log=log)
    except LLMUnavailable as e:
        raise SystemExit(f"LLM unavailable ({e}). Pages are cached: rerun later with --reuse-pages, "
                         f"point LLM_BASE_URL at another model, or use --guess for laya-only (unvalidated) values.")
    if not known or report.get("changed"):  # new, or improved by the coverage check
        os.makedirs("selectors", exist_ok=True)
        with open(sel_path, "w", encoding="utf-8") as fh:
            json.dump({"selectors": report["selectors"], "fields": report["fields"],
                       "coverage_checked": report["coverage_checked"], "fields_in_plan": names,
                       "validated_on": (known or {}).get("validated_on") or time.strftime("%Y-%m-%d")}, fh, indent=2)
    log(f"extracted in {time.time() - t1:.0f}s  [{time.time() - t0:.0f}s total, {llm.calls['n']} LLM calls]")

    items = [{"url": u, **got[u]} for u in listed if u in got]
    names = [x["name"] for x in plan["fields"]]
    for x in items[:12]:
        log("  " + " | ".join(str(x.get(n) if not isinstance(x.get(n), list) else "; ".join(x[n][:2]))[:38] for n in names[:4]))
    filled = {n: sum(1 for x in items if x.get(n)) for n in names}
    log("filled: " + ", ".join(f"{n} {c}/{len(items)}" for n, c in filled.items()))

    result = {"site": a.site, "prompt": a.prompt, "plan": plan, "listing": hub, "paging": info, "fields": report["fields"],
              "selectors": report["selectors"],
              "filled": filled, "seconds": round(time.time() - t0), "items": items}
    if a.out and a.out.endswith(".csv"):
        with open(a.out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=["url"] + names + [f"{n}_confidence" for n in names])
            w.writeheader()
            for x in items:
                row = {k: ("; ".join(v) if isinstance(v, list) else v) for k, v in x.items() if k in names or k == "url"}
                row.update({f"{n}_confidence": x["_confidence"].get(n) for n in names})
                w.writerow(row)
    elif a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
    print(json.dumps({k: v for k, v in result.items() if k not in ("items", "plan")}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
