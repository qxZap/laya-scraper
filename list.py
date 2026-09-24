"""Stage 2: walk the publications list page by page and pull the facts out of every publication.

    python list.py https://odi.org/en/                          # find the list with scrape.py first
    python list.py --hub https://odi.org/en/publications/        # or start from a known list page
    python list.py https://odi.org/en/ --max-items 100 --out odi.csv

How to reach the next page is worked out per site. Nothing is hard-coded:
  next-link  a rel=next / "Next" / "›" / "Load more" link, or the numbered link to page N+1
  load-more  a JS button or infinite scroll: the real browser clicks/scrolls until it has enough
  probe      no visible control: try ?page= / ?paged= / ?p= / /page/N/ and keep what returns new items
  view-all   a curated landing page: follow its "View all ..." link and start over there
Facts come from structured metadata first (JSON-LD, the citation_* tags Google Scholar reads,
OpenGraph), then from the page itself. laya labels what kind of publication each one is.
"""
import argparse, csv, json, math, re, sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from scrape import DATE, Brain, Browser, crawl, fetch_requests, host, parse

NEXT = re.compile(r"^(next( page)?|older( posts| entries)?|(load|show|view|see) more|more results)?\s*[›»>→]*$", re.I)
VIEW_ALL = re.compile(r"\b(view|see|browse|show) all\b|\bfull (list|archive)\b|\ball (publications|research|reports)\b", re.I)
TOTAL = re.compile(r"\b(\d[\d,.]{0,9})\s+(results|items|publications|records|documents|reports)\b", re.I)
# ponytail: zero-shot labels, roughly right on reports/briefs, shaky on the rest; fine-tune laya on
# (page -> kind) pairs once there are labelled examples
KINDS = {
    "report": "a long research report or study, usually with a PDF",
    "brief": "a short policy brief or briefing paper",
    "working_paper": "a working paper or discussion paper",
    "academic": "a peer-reviewed paper published in an academic journal",
    "book": "a book or book chapter",
    "commentary": "a web commentary, blog post, opinion or analysis piece",
    "media": "a podcast episode, video or webinar",
    "event": "an event",
    "other": "something else",
}
# social handles, icons and "See more" links that sit next to author names
NOT_A_NAME = re.compile(r"[\d@/:+|]|\b(more|icon|streamline|bluesky|twitter|linkedin|email|profile)\b", re.I)


class Fetcher:
    """requests first; the real browser when blocked, JS-rendered, or when a list has to be expanded."""

    def __init__(self, tabs):
        self.browser, self.pool, self.walled = Browser(tabs), ThreadPoolExecutor(tabs), False

    def get(self, urls, more=0):
        out = {}
        if not self.walled and not more:
            got = dict(zip(urls, self.pool.map(fetch_requests, urls)))
            if sum(r.get("status") in (403, 429, 503) for r in got.values()) > len(urls) / 2:
                self.walled = True
            for u, r in got.items():
                if "error" not in r and r["status"] < 400:
                    p = parse(r["final_url"], r["html"])
                    if len(p["links"]) >= 5 and p["text_len"] >= 1500:  # else thin/JS shell
                        out[u] = {**r, "via": "requests"}
        rest = [u for u in urls if u not in out]
        for u, r in self.browser.get_many([json.dumps({"url": u, "more": more}) for u in rest]).items():
            if "error" not in r and r["status"] < 400:
                out[u] = {**r, "via": "browser"}
            else:
                print(f"  ! {u}: {r.get('error') or r['status']}", file=sys.stderr)
        return out

    def close(self):
        self.browser.close()
        self.pool.shutdown()


def slugish(u):
    last = urlsplit(u).path.rstrip("/").rsplit("/", 1)[-1]
    return last.count("-") >= 2 or bool(re.search(r"\d{4,}", last))


def item_group(p):
    """The list's entries: the link group with the most titled, slug-like URLs (not facets or tags)."""
    score = lambda g: sum(len(t) > 20 and slugish(u) for u, t in g.items())
    return max(p["groups"].items(), key=lambda kv: score(kv[1]), default=("", {}))


def anchors(url, html):
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if a["href"].startswith(("#", "javascript", "mailto")):
            continue
        t = " ".join((a.get_text(" ", strip=True) or a.get("aria-label") or a.get("title") or "").split())
        yield a, t, urljoin(url, a["href"])


def find_next(url, html, pageno):
    """URL of the next page from rel=next, a Next/›/Load-more link, or the numbered link to page N+1."""
    soup = BeautifulSoup(html, "html.parser")
    rel = soup.select_one('a[rel~="next"][href], link[rel~="next"][href]')
    if rel:
        return urljoin(url, rel["href"])
    numbered = None
    for a, t, u in anchors(url, html):
        if u.split("#")[0] == url.split("#")[0]:
            continue
        if (t and NEXT.match(t)) or re.search(r"\bnext page\b", a.get("aria-label", ""), re.I):
            return u
        if re.fullmatch(rf"(page\s*)?{pageno + 1}", t, re.I):
            numbered = numbered or u
    return numbered


def page_count(next_url, html, url):
    """Last page number, from the pager links shaped like the next link (?page=407, "Last page" too)."""
    shape, num = re.sub(r"\d+", "#", next_url), lambda u: int(re.findall(r"\d+", u)[-1])
    nums = [num(u) for _, _, u in anchors(url, html) if re.sub(r"\d+", "#", u) == shape]
    # next link of page 1 is page 2; if it says ?page=1 the site counts from 0
    return max(nums) + 2 - num(next_url) if nums else None


def probes(url, n):
    """Common page-N URL conventions, tried when the page shows no pagination control at all."""
    s = urlsplit(url)
    q = dict(parse_qsl(s.query))
    for key, val in (("page", n), ("page", n - 1), ("paged", n), ("p", n), ("pg", n)):
        yield f"?{key}={{}}", urlunsplit(s._replace(query=urlencode({**q, key: val}))), val - n
    yield "/page/{}/", urlunsplit(s._replace(path=s.path.rstrip("/") + f"/page/{n}/")), 0


def text_of(tag):
    return " ".join(tag.get_text(" ", strip=True).split()) if tag else ""


def facts(url, html):
    """Title, date, authors, summary, PDF, DOI, topics: metadata first, page content as fallback."""
    soup = BeautifulSoup(html, "html.parser")
    meta = {}
    for m in soup.find_all("meta"):
        k = (m.get("name") or m.get("property") or m.get("itemprop") or "").lower()
        if k and m.get("content"):
            meta.setdefault(k, []).append(m["content"].strip())
    first = lambda *ks: next((meta[k][0] for k in ks if k in meta), "")

    ld, stack = [], []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            stack.append(json.loads(s.string or ""))
        except ValueError:
            pass
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            ld.append(x)
            stack.extend(x.get("@graph", []))
    doc = next((x for x in ld if re.search(r"Article|Report|Book|Chapter|CreativeWork|Thesis", str(x.get("@type")))), {})

    def names(v):
        v = v if isinstance(v, list) else [v]
        return [x.get("name", "") if isinstance(x, dict) else str(x) for x in v if x]

    time = soup.find("time")
    date = (first("citation_publication_date", "citation_date", "dc.date", "article:published_time", "datepublished")
            or doc.get("datePublished") or (time.get("datetime") or text_of(time) if time else ""))
    if not date:
        m = DATE.search(soup.get_text(" ", strip=True))
        date = m.group() if m else ""
    authors = (meta.get("citation_author") or names(doc.get("author")) or meta.get("author")
               or [t for a in soup.select('a[rel="author"], [class*="author"] a') for t in [text_of(a)]
                   if 2 <= len(t.split()) <= 5 and t[:1].isupper() and not NOT_A_NAME.search(t)])
    authors = list(dict.fromkeys(a.strip() for a in authors if a and len(a.strip()) < 80))[:12]
    pdf = first("citation_pdf_url") or next((urljoin(url, a["href"]) for a in soup.find_all("a", href=True)
                                            if urlsplit(a["href"]).path.lower().endswith(".pdf")), "")
    doi = re.search(r"10\.\d{4,9}/[^\s\"'<>]+", " ".join(meta.get("citation_doi", []) + meta.get("dc.identifier", []))
                    or " ".join(a["href"] for a in soup.select('a[href*="doi.org/10."]')))
    kw = meta.get("article:tag", []) + [k for v in meta.get("keywords", []) for k in v.split(",")]
    kw += doc.get("keywords", []) if isinstance(doc.get("keywords"), list) else str(doc.get("keywords", "")).split(",")
    return {
        "title": (first("citation_title", "dc.title") or doc.get("headline") or doc.get("name")
                  or first("og:title") or text_of(soup.h1) or text_of(soup.title))[:300],
        "date": re.sub(r"^(\d{4}-\d{2}-\d{2})T.*", r"\1", str(date).strip())[:40],
        "authors": authors,
        "summary": (first("citation_abstract", "description", "og:description", "dc.description")
                    or doc.get("description") or "")[:600],
        "pdf": pdf,
        "doi": doi.group().rstrip(".,;)") if doi else "",
        "topics": list(dict.fromkeys(k.strip() for k in kw if k.strip()))[:10],
        "schema_type": str(doc.get("@type", "") or first("og:type")),
    }


def walk(f, hub, max_items):
    """Collect up to max_items list entries from hub, working out how to page through them."""
    first_page = f.get([hub], more=1).get(hub)
    if not first_page:
        raise SystemExit(f"could not load {hub}")
    p = parse(first_page["final_url"], first_page["html"])
    gpath, got = item_group(p)
    items = dict(got)
    info = {"list": first_page["final_url"], "mode": "single-page", "item_pattern": gpath, "pages_fetched": 1,
            "total_results_est": None, "total_pages_est": None}
    # "53038 results found" / "of 369 results" states the total; bare "286 results" are usually facet
    # counts next to a filter, so those only count as a fallback (the largest)
    text = BeautifulSoup(first_page["html"], "html.parser").get_text(" ", strip=True)
    counts = [(bool(re.search(r"^\s*found|of\s*$", text[m.end():m.end() + 7] + "|" + text[max(0, m.start() - 4):m.start()])),
               int(re.sub(r"\D", "", m.group(1)) or 0)) for m in TOTAL.finditer(text)]
    info["total_results_est"] = max(counts, default=(0, None))[1]
    print(f"  page 1: {len(items)} items at {gpath} via {first_page['via']}", file=sys.stderr)

    def more_items(page):
        return {u: t for u, t in parse(page["final_url"], page["html"])["groups"].get(gpath, {}).items() if u not in items}

    nxt = find_next(first_page["final_url"], first_page["html"], 1)
    grown = (first_page.get("more") or {}).get("rounds", 0)
    if nxt:
        info.update(mode="next-link", next_example=nxt, total_pages_est=page_count(nxt, first_page["html"], first_page["final_url"]))
        pageno, url = 1, nxt
        while url and len(items) < max_items:
            page = f.get([url], more=1 if f.walled else 0).get(url)
            new = more_items(page) if page else {}
            if not new:
                break
            items.update(new)
            pageno += 1
            info["pages_fetched"] = pageno
            print(f"  page {pageno}: +{len(new)} items ({len(items)})", file=sys.stderr)
            url = find_next(page["final_url"], page["html"], pageno)
    elif grown or first_page["via"] == "requests":
        # JS "load more" / infinite scroll; a requests-served page may still hide one, so check in the browser
        rounds = math.ceil(max_items / max(1, len(got))) + 1
        page = f.get([hub], more=rounds).get(hub)
        new = more_items(page) if page else {}
        if new:
            items.update(new)
            info.update(mode="load-more", rounds=page["more"]["rounds"], clicks=page["more"]["clicks"])
            print(f"  load-more: {page['more']['rounds']} rounds, +{len(new)} items ({len(items)})", file=sys.stderr)
    if info["mode"] == "single-page":
        # a curated landing page often links to the real archive
        for a, t, u in anchors(first_page["final_url"], first_page["html"]):
            if VIEW_ALL.search(t) and host(u) == host(hub) and u.rstrip("/") != hub.rstrip("/"):
                print(f"  no pagination here; following '{t}' -> {u}", file=sys.stderr)
                sub_items, sub = walk(f, u, max_items)
                return sub_items, {**sub, "mode": "view-all -> " + sub["mode"], "landing": hub}
        for shape, cand, offset in probes(first_page["final_url"], 2):
            page = f.get([cand], more=1 if f.walled else 0).get(cand)
            new = more_items(page) if page else {}
            if len(new) >= 3:
                info.update(mode="probe", next_example=cand, shape=shape)
                items.update(new)
                print(f"  probe {shape}: +{len(new)} items ({len(items)})", file=sys.stderr)
                n = 3
                while len(items) < max_items:
                    url = next(c for s, c, _ in probes(first_page["final_url"], n) if s == shape and _ == offset)
                    page = f.get([url], more=1 if f.walled else 0).get(url)
                    new = more_items(page) if page else {}
                    if not new:
                        break
                    items.update(new)
                    n += 1
                info["pages_fetched"] = n - 1
                break
    return dict(list(items.items())[:max_items]), info


def main():
    for stream in (sys.stdout, sys.stderr):  # titles are full of ’ – ö; a cp1252 console would crash
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="site to search, or the list page itself with --hub")
    ap.add_argument("--hub", action="store_true", help="url is already the list page; skip discovery")
    ap.add_argument("--target", default="publications (research reports, papers, briefs)")
    ap.add_argument("--max-items", type=int, default=40)
    ap.add_argument("--max-pages", type=int, default=30, help="crawl budget for discovery")
    ap.add_argument("--tabs", type=int, default=4)
    ap.add_argument("--out", help="write items to .csv, or the full result to .json")
    a = ap.parse_args()

    brain = Brain(a.target)
    hub = a.url
    if not a.hub:
        print("stage 1: finding the list ...", file=sys.stderr)
        hub = crawl(a.url, a.target, a.max_pages, 3, "auto", a.tabs, brain=brain)["answer"]
        print(f"stage 1: list is {hub}", file=sys.stderr)

    print("stage 2: paging through the list ...", file=sys.stderr)
    f = Fetcher(a.tabs)
    try:
        listed, info = walk(f, hub, a.max_items)
        print(f"stage 3: reading {len(listed)} publications ...", file=sys.stderr)
        pages = f.get(list(listed))
    finally:
        f.close()

    items = []
    for u, list_title in listed.items():
        if u in pages:
            items.append({"url": u, "list_title": list_title, **facts(pages[u]["final_url"], pages[u]["html"]),
                          "via": pages[u]["via"]})
    # the list's own label ("Research paper …") is the strongest hint; schema.org @type misleads ("NewsArticle")
    states = [f"How the site lists it: {x['list_title'][:200]}\nTitle: {x['title']}\n"
              f"PDF download: {'yes' if x['pdf'] else 'no'}\nSummary: {x['summary'][:300]}" for x in items]
    qs = {"kind": {"type": "choice", "instructions": "What kind of publication is this?", "criteria": KINDS}}
    for x, ans in zip(items, brain._run(states, qs)):
        x["kind"] = ans["kind"]["choice"]
        x["kind_confidence"] = ans["kind"]["answer_confidence"]

    for x in items:
        print(f"  {x['date'][:10]:10} {x['kind']:15} {x['title'][:70]:70} {'; '.join(x['authors'][:2])[:40]}", file=sys.stderr)
    result = {"site": a.url, **info, "items_found": len(items), "items": items}
    if a.out and a.out.endswith(".csv"):
        with open(a.out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(items[0]) if items else ["url"])
            w.writeheader()
            w.writerows({k: "; ".join(v) if isinstance(v, list) else v for k, v in x.items()} for x in items)
    elif a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
    print(json.dumps({k: v for k, v in result.items() if k != "items"}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
