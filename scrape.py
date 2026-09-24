"""Find where a site keeps its publications (or any --target), and classify every place on the way.

Best-first, batched crawl: laya scores every discovered link ("is this the nav link to <target>?")
and each round takes the most promising URLs. They are fetched in parallel with requests, falling
back to puppeteer-real-browser (multi-tab, passes Cloudflare) when a page is blocked, JS-rendered,
or looks like a list whose items didn't load. laya then classifies the batch in one pass: page
role, what it holds, and how on-topic it is; page structure decides whether it is a list.

    python scrape.py https://odi.org/en/
"""
import argparse, heapq, json, os, re, subprocess, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp[34]|csv|xml|rss|ics)$", re.I)
DATE = re.compile(r"\b\d{1,2}\s+[A-Za-z]{3,9}\.?\s+(19|20)\d{2}\b|\b[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+(19|20)\d{2}\b|\b(19|20)\d{2}-\d{2}-\d{2}\b")


def norm(url):
    # ponytail: query dropped so ?page=/?filter= variants don't flood the crawl; sites that
    # route pages by query (?p=123) need it kept.
    s = urlsplit(url)
    return urlunsplit((s.scheme.lower(), s.netloc.lower(), s.path or "/", "", ""))


def host(url):
    return urlsplit(url).netloc.lower().removeprefix("www.")


class Browser:
    """Lazy render.js process (one browser, N tabs); only started if some page needs it."""
    proc = None

    def __init__(self, tabs):
        self.tabs = tabs

    def get_many(self, urls):
        if not urls:
            return {}
        if not self.proc:
            self.proc = subprocess.Popen(["node", "render.js"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         text=True, encoding="utf-8", bufsize=1,
                                         env={**os.environ, "TABS": str(self.tabs)})
        self.proc.stdin.write("".join(u + "\n" for u in urls))
        self.proc.stdin.flush()
        out = {}
        for _ in urls:  # answers arrive in completion order
            r = json.loads(self.proc.stdout.readline())
            out[r["url"]] = r
        return out

    def close(self):
        if self.proc:
            self.proc.stdin.close()
            self.proc.wait(timeout=60)


def fetch_requests(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        return {"url": url, "final_url": r.url, "status": r.status_code, "html": r.text}
    except requests.RequestException as e:
        return {"url": url, "error": str(e)}


def parent(path):
    """Path pattern of a link's folder; numeric segments wildcarded so /2025/12/x and /2026/01/y group."""
    return "/" + "".join(("*" if s.isdigit() else s) + "/" for s in path.strip("/").split("/")[:-1])


def parse(url, html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    base = norm(url)
    links, pdfs = {}, 0
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if not href.startswith("http"):
            continue
        if SKIP_EXT.search(urlsplit(href).path) or "/cdn-cgi/" in href:
            pdfs += urlsplit(href).path.lower().endswith(".pdf")
            continue
        u = norm(href)
        if host(u) != host(base) or u == base:
            continue
        text = " ".join((a.get_text(" ", strip=True) or a.get("aria-label") or a.get("title") or "").split())[:120]
        if len(text) >= len(links.get(u, "")):
            links[u] = text
    title = " ".join((soup.title.get_text() if soup.title else "").split())[:150]
    # what the page *holds*: links outside site chrome, grouped by parent path; biggest group = the list
    for t in soup.select("header, nav, footer, aside, [role=navigation], [role=banner], [role=contentinfo]"):
        t.decompose()
    groups = {}
    for a in soup.find_all("a", href=True):
        u = norm(urljoin(url, a["href"]))
        if u in links:
            groups.setdefault(parent(urlsplit(u).path), {})[u] = links[u]
    # real list entries carry titles; short labels ("About", "Contact") are section nav
    gpath, items = max(groups.items(), key=lambda g: sum(len(t) > 20 for t in g[1].values()), default=("", {}))
    text = soup.get_text(" ", strip=True)
    return {
        "title": title,
        "h1": " ".join(soup.h1.get_text(" ", strip=True).split())[:150] if soup.h1 else "",
        "heads": [h.get_text(" ", strip=True)[:80] for h in soup.find_all(["h2", "h3"])][:12],
        "links": links,
        "items": items,
        "gpath": gpath,
        "pdfs": pdfs,
        "dates": len(DATE.findall(text)),
        "paged": bool(soup.select('a[rel~="next"], link[rel~="next"], [class*="pagination"], [class*="pager"]')),
        "text_len": len(text),
    }


def kids(p):
    return sum(len(t) > 20 for t in p["items"].values())


def page_state(url, p):
    ex = [t for t in p["items"].values() if len(t) > 15][:8]
    return "\n".join([
        f"URL path: {urlsplit(url).path}",
        f"Title: {p['title']}",
        f"Main heading: {p['h1']}",
        f"Section headings: {' | '.join(p['heads'])}",
        f"Main content lists {len(p['items'])} similar linked items (at {p['gpath'] or '-'}); "
        f"pagination: {'yes' if p['paged'] else 'no'}; PDF downloads: {p['pdfs']}; dates shown: {p['dates']}",
        f"Listed items: {' ; '.join(ex) or '(none)'}",
    ])


class Brain:
    def __init__(self, target):
        import warnings
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        warnings.filterwarnings("ignore")  # laya calibration notices; noise for a crawl log
        from laya import Router
        import torch
        self.router = Router()
        print(f"laya on {'cuda: ' + torch.cuda.get_device_name() if torch.cuda.is_available() else 'cpu'}", file=sys.stderr)
        self.link_q = {"leads": {"type": "noul",
                                 "instructions": f"Is this link a website navigation link to the section that lists all of the site's {target}?"}}
        self.page_q = {
            "hub": {"type": "noul",
                    "instructions": f"Is this page about the site's {target}?"},
            "role": {"type": "choice", "instructions": "What kind of page is this?", "criteria": {
                "listing": "an index, archive or search page listing many items",
                "item": "a single item: one article, report, event, person or job",
                "hub": "a homepage or topic hub mixing many kinds of content",
                "info": "static information: about, contact, policies, donate"}},
            "holds": {"type": "choice", "instructions": "What does this page mainly contain or list?", "criteria": {
                "publications": "research publications, reports, papers, briefs",
                "news": "news, blogs, insights, opinion, press releases",
                "events": "events, conferences, webinars",
                "people": "staff, experts, team, leadership",
                "jobs": "careers, vacancies",
                "media": "podcasts, videos, multimedia",
                "projects": "projects, programmes, topics, themes",
                "organisation": "information about the organisation",
                "mixed": "a mix of many content types"}},
        }

    def _run(self, states, qs):
        if not states:
            return []
        return [r["answers"] for r in self.router.predict_batch([{"state": s, "questions": qs} for s in states], batch_size=32)]

    def score_links(self, links, under):
        states = [f"Link text: {t or '(none)'}\nURL path: {urlsplit(u).path}\n"
                  f"Path depth: {urlsplit(u).path.strip('/').count('/') + 1}\n"
                  f"Known site pages under this path: {under(u)}" for u, t in links]
        return [a["leads"]["noul"] for a in self._run(states, self.link_q)]

    def classify(self, states):
        return [(a["hub"]["noul"], a["role"]["choice"], a["holds"]["choice"]) for a in self._run(states, self.page_q)]


def crawl(start, target, max_pages, max_depth, mode, tabs):
    brain, browser, pool = Brain(target), Browser(tabs), ThreadPoolExecutor(tabs)
    start = norm(start)
    heap, seen, places, known, listed, done, n = [(-1.0, 0, start, 0)], {start}, [], set(), set(), set(), 0
    inlinks = Counter()
    scope = urlsplit(start).path.rstrip("/") + "/"
    walled = mode == "always"  # flips on once the site bot-walls plain requests
    # ponytail: O(known) prefix scan per call, fine for a few thousand URLs; trie if it grows
    under = lambda u: sum(x.startswith(u) and x != u for x in known)

    def usable(r):
        return "error" not in r and r["status"] < 400

    try:
        while heap and len(places) < max_pages:
            batch = [heapq.heappop(heap) for _ in range(min(tabs * 2, len(heap), max_pages - len(places)))]
            urls = [b[2] for b in batch]

            pages = {}
            if not walled:
                got = dict(zip(urls, pool.map(fetch_requests, urls)))
                if mode == "auto" and sum(r.get("status") in (403, 429, 503) for r in got.values()) > len(urls) / 2:
                    walled = True
                    print("  site bot-walls plain requests -> browser from now on", file=sys.stderr)
                for u, r in got.items():
                    if usable(r):
                        p = parse(r["final_url"], r["html"])
                        if len(p["links"]) >= 5 and p["text_len"] >= 1500:  # else thin/JS shell -> browser
                            pages[u] = (r["final_url"], p, "requests")
            if mode != "never":
                for u, r in browser.get_many([u for u in urls if u not in pages]).items():
                    if usable(r):
                        pages[u] = (r["final_url"], parse(r["final_url"], r["html"]), "browser")
                    else:
                        print(f"  ! {u}: {r.get('error') or r['status']} {r.get('title', '')}", file=sys.stderr)

            pages = {u: v for u, v in pages.items() if host(v[0]) == host(start)}
            for _, p, _ in pages.values():
                known.update(p["links"])
            verdicts = dict(zip(pages, brain.classify([page_state(f, p) for f, p, _ in pages.values()])))

            # on-topic, the site has pages beneath it, yet static HTML lists none -> JS-loaded list
            redo = [u for u, (f, p, via) in pages.items() if mode == "auto" and via == "requests"
                    and verdicts[u][0] > 0.5 and kids(p) < 3 and under(norm(f)) >= 2]
            fixed = {u: r for u, r in browser.get_many(redo).items() if usable(r)}
            for u, r in fixed.items():
                pages[u] = (r["final_url"], parse(r["final_url"], r["html"]), "browser")
                known.update(pages[u][1]["links"])
            verdicts.update(zip(fixed, brain.classify([page_state(*pages[u][:2]) for u in fixed])))

            new = []
            for neg, _, url, depth in batch:
                if url not in pages or norm(pages[url][0]) in done:  # redirects can land on a visited page
                    continue
                final, p, via = pages[url]
                done.add(norm(final))
                inlinks.update(p["links"].keys())
                hub, role, holds = verdicts[url]
                k = kids(p)
                # laya judges *what* (semantics), structure judges *is it a list*.
                # ponytail: hand-tuned blend; learn weights once there are labelled sites
                score = hub * (0.2 + 0.8 * min(1.0, k / 10 + 0.2 * p["paged"]))
                places.append({"url": final, "via": via, "depth": depth, "role": role, "holds": holds,
                               "hub": hub, "items": k, "score": round(score, 4), "prio": round(-neg, 4)})
                print(f"[{len(places):3}] {score:.2f} hub={hub:.2f} {role:7} {holds:13} items={k:3} {via:8} {final}",
                      file=sys.stderr)
                g = p["gpath"]
                if k >= 5 and g not in listed and g not in ("/", scope):
                    # confirmed list: its entries are that place's contents, not new places
                    listed.add(g)
                    heap = [(e[0] * 0.2 if parent(urlsplit(e[2]).path) == g else e[0],) + e[1:] for e in heap]
                    heapq.heapify(heap)
                if depth < max_depth:
                    for u, t in p["links"].items():
                        if u not in seen:
                            seen.add(u)
                            new.append((u, t, depth + 1))
            # one laya pass scores every link discovered this round
            for (u, _, d), s in zip(new, brain.score_links([(u, t) for u, t, _ in new], under)):
                s *= 0.4 + 0.6 * min(under(u), 5) / 5  # index links have many pages beneath them
                if parent(urlsplit(u).path) in listed:
                    s *= 0.2
                if not urlsplit(u).path.startswith(scope):  # e.g. start /en/ -> /fr/ mirror is out of scope
                    s *= 0.3
                n += 1
                heapq.heappush(heap, (-s, n, u, d))
    finally:
        browser.close()
        pool.shutdown()

    # navigational prominence: real section indexes are linked from (nearly) every page's menu
    for r in places:
        r["linked_from"] = round(min(1.0, inlinks[norm(r["url"])] / len(places)), 3)
        r["score"] = round(r["score"] * (0.6 + 0.4 * r["linked_from"]), 4)
    # the start page is where we came from, not an answer, unless nothing else qualifies
    ranked = sorted(places, key=lambda r: (r["depth"] == 0, -r["score"], len(urlsplit(r["url"]).path)))
    # map of places: per content type, the biggest list that the site menu links to
    sections = {}
    for r in sorted(places, key=lambda r: -r["items"] * (0.1 + r["linked_from"])):
        if r["items"] >= 5 and r["depth"] > 0:
            sections.setdefault(r["holds"], r["url"])
    if ranked:  # the target's own slot belongs to the answer
        sections[ranked[0]["holds"]] = ranked[0]["url"]
    return {"start": start, "target": target, "answer": ranked[0]["url"] if ranked else None,
            "sections": sections, "top": ranked[:5], "places": places}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--target", default="publications (research reports, papers, briefs)")
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--tabs", type=int, default=4, help="parallel fetches / browser tabs")
    ap.add_argument("--browser", choices=["auto", "always", "never"], default="auto")
    ap.add_argument("--out", help="write full JSON report here")
    a = ap.parse_args()
    res = crawl(a.url, a.target, a.max_pages, a.max_depth, a.browser, a.tabs)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, ensure_ascii=False)
    print(json.dumps({k: res[k] for k in ("answer", "sections", "top")}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
