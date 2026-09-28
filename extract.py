"""Schema-driven field extraction: learn one selector per field per site, prove it, then apply it everywhere.

1. Decompose. Each page becomes a tree: page metadata (meta tags, JSON-LD) and the main content with site
   chrome, related-item cards and hidden text removed. "Label: value" blocks (<strong>Filed:</strong> ...,
   <dt>/<dd>, table rows) become individual labelled facts.
2. Map. The LLM sees two pages as numbered outlines and maps each schema field to an element.
   The element becomes a selector: a CSS path, a meta/JSON-LD key, or a block path plus the fact's label.
3. Validate. The selectors run on sample pages (two of them unseen) and the LLM grades every value against
   the page. Failing fields go back to the LLM with what went wrong, for a couple of repair rounds.
4. Apply. Only validated selectors produce values, on every page, with type checks (dates parse, links are
   links). laya scores each value ("is this exactly the <field>?") as a per-item confidence signal.
Fields that never validate stay empty: a blank is honest, a wrong value is not. Opt-in `guess` mode fills
them with laya's own top-down DOM walk ("does this block contain the <field>?" ... down to the element).
A few LLM calls per site, none per item; the per-item work is CSS selection plus one batched laya pass.
"""
import json, re, time
from html import unescape
from collections import Counter
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from urllib.parse import urlsplit

from scrape import norm, parent

CHROME = ("script, style, noscript, template, svg, nav, footer, aside, form, iframe, dialog, "
          "[role=navigation], [role=contentinfo], [role=dialog], [aria-hidden=true], [hidden], "
          ".sr-only, .visually-hidden, .screen-reader-text, [class*=visually-hidden], [class*=skip-link]")
# chrome that isn't marked up as such: <div id="footer">, breadcrumb bars, cookie banners, share buttons
CHROME_NAME = re.compile(r"(^|[-_\s])(footer|breadcrumbs?|cookies?|newsletter|social|share|sharing|sidebar|"
                         r"related|recommended|subscribe|consent)([-_\s]|$)", re.I)
CLASS_OK = re.compile(r"^[A-Za-z_-][\w-]{0,39}$")
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def clean(s):
    return " ".join((s or "").split())


class Node:
    """A candidate in the page tree: an element, a metadata value, or a group of either."""

    def __init__(self, text, el=None, sel="", value=None, kids=None, attrs=""):
        self.text, self.el, self.sel, self.value, self._kids, self.attrs = text, el, sel, value, kids, attrs

    def kids(self):
        if self._kids is None and self.el is None:
            return []
        if self._kids is None:
            self._kids = []
            for c in self.el.find_all(True, recursive=False):
                if clean(c.get_text(" ")) or c.name == "a" and c.get("href"):
                    self._kids.append(el_node(c))
        return self._kids

    def state(self):
        if self.el is None:
            kind = ("Labelled fact on the page" if self.sel.startswith("kv:") else
                    "Text on the page" if self.sel.startswith("re:") else "Page metadata")
            return f"{kind}: {self.text[:400]}"
        return f"Element: {self.attrs}\nText: {self.text[:350]}"


def stable_classes(el):
    return [c for c in el.get("class", []) if CLASS_OK.match(c) and not re.search(r"\d", c)][:2]


def css_path(el, stop):
    parts = []
    while isinstance(el, Tag) and el is not stop and el.name not in ("body", "html", "[document]"):
        if el.get("id") and CLASS_OK.match(el["id"]) and not re.search(r"\d", el["id"]):
            parts.append(f"{el.name}#{el['id']}")
            break
        parts.append(el.name + "".join("." + c for c in stable_classes(el)))
        el = el.parent
    return " > ".join(reversed(parts))


def el_node(el):
    # wrapper chains (div > div > div > h1) collapse to the innermost element that still branches
    while True:
        kids = [c for c in el.find_all(True, recursive=False) if clean(c.get_text(" "))]
        own = clean("".join(t for t in el.find_all(string=True, recursive=False)))
        if len(kids) == 1 and not own:
            el = kids[0]
        else:
            break
    extra = []
    if el.name == "a" and el.get("href"):
        extra.append(f"link to {el['href'][:120]}")
    if el.name == "time" and el.get("datetime"):
        extra.append(f"datetime={el['datetime']}")
    for a in el.find_all("a", href=True, limit=3) if el.name != "a" else []:
        if re.search(r"\.pdf\b|download", a["href"], re.I):
            extra.append(f"contains link to {a['href'][:120]}")
    label = label_of(el)
    if label:
        extra.insert(0, f"labelled {label!r}")
    cls = " ".join(el.get("class", [])[:4])
    attrs = f"<{el.name}{' class=' + repr(cls) if cls else ''}>" + (" " + "; ".join(extra) if extra else "")
    return Node(clean(el.get_text(" ")), el=el, attrs=attrs)


def label_of(el):
    """The label a key-value layout puts next to a value: <dt>/<th> before it, or a short previous sibling."""
    if el.name == "td":
        row = el.find_parent("tr")
        th = row.find("th") if row else None
        if th is not None:
            return clean(th.get_text(" "))[:50]
    prev = el.find_previous_sibling()
    if prev is not None:
        t = clean(prev.get_text(" "))
        if prev.name in ("dt", "th", "label", "strong", "b") or 0 < len(t) <= 30 and t.endswith(":"):
            return t[:50]
    return ""


def jsonld_values(soup):
    out = {}

    def walk(x, path):
        if isinstance(x, dict):
            for k, v in x.items():
                if k not in ("@context", "@id"):
                    walk(v, f"{path}.{k}" if path else k)
        elif isinstance(x, list):
            for v in x:
                walk(v, path)
        elif x not in (None, ""):
            out.setdefault(path, []).append(clean(unescape(str(x))))

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(s.string or "")
        except ValueError:
            continue
        for item in (d.get("@graph", [d]) if isinstance(d, dict) else d):
            # site furniture, not the item: breadcrumbs, the site itself, its search box, logos
            if isinstance(item, dict) and re.search(r"Breadcrumb|WebSite|SearchAction|SiteNavigation|ImageObject",
                                                    str(item.get("@type"))):
                continue
            walk(item, "")
    return out


def page_tree(html, url="", item_pattern=""):
    """Virtual root: [page metadata group, main content]. Returns (root, soup, content element).
    item_pattern: path pattern of the listing's items; blocks linking to *other* such items are
    "related content" cards whose titles and dates would otherwise look like this item's."""
    soup = BeautifulSoup(html, "html.parser")
    metas = {}
    for m in soup.find_all("meta"):
        k = (m.get("name") or m.get("property") or m.get("itemprop") or "").strip()
        if k and m.get("content") and not re.search(r"^(viewport|theme-color|robots|google|msapplication|fb:|twitter:image|og:image|referrer|format-detection)|nonce|csrf|token|verification", k, re.I):
            metas.setdefault(k, []).append(clean(unescape(m["content"])))
    meta_nodes = [Node(f"{k}: {'; '.join(v)[:300]}", sel=f"meta:{k}", value=v) for k, v in metas.items()]
    meta_nodes += [Node(f"{k}: {'; '.join(v)[:300]}", sel=f"ld:{k}", value=v)
                   for k, v in jsonld_values(soup).items() if len("".join(v)) < 2000]
    for t in soup.select(CHROME):
        t.decompose()
    for t in soup.find_all(True):
        if not t.decomposed and t.attrs is not None and t.name not in ("html", "body", "main") and \
                CHROME_NAME.search(" ".join([t.get("id") or ""] + (t.get("class") or []))) and \
                t.find(["main", "article", "h1"]) is None:  # "content-sidebar-wrap" holds the whole page: keep it
            t.decompose()
    if item_pattern:
        me = norm(url)
        for a in soup.find_all("a", href=True):
            if a.decomposed:  # inside a card removed a moment ago
                continue
            u = norm(urljoin(url, a["href"]))
            # a card shows another item's *title*; a short or numeric link ("20170243123") is a reference in a fact
            label = clean(a.get_text(" "))
            if len(label) < 20 or sum(ch.isdigit() for ch in label) > len(label) / 3:
                continue
            if a.parent is not None and u != me and parent(urlsplit(u).path) == item_pattern:
                card = a
                while card.parent is not None and card.parent.name not in ("body", "main", "article")                         and len(clean(card.parent.get_text(" "))) < 500:
                    card = card.parent
                card.decompose()
    for h in soup.find_all("header"):  # site header yes, an article's own header no
        if not h.find_parent(["main", "article"]):
            h.decompose()
    # <main> only when it really holds the page: some sites put a small widget in <main> and the item outside it
    body = soup.body or soup
    main = soup.find("main") or soup.find(attrs={"role": "main"})
    content = main if main is not None and len(main.get_text()) >= 0.5 * len(body.get_text()) else body
    kids = [Node("page metadata (meta tags, structured data): " + " | ".join(n.text[:60] for n in meta_nodes[:25]),
                 kids=meta_nodes)] if meta_nodes else []
    kids.append(el_node(content))
    return Node("whole page", kids=kids), soup, content


def to_iso(s):
    s = clean(s)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s) or re.search(r"(\d{4})/(\d{2})/(\d{2})", s)
    if m:
        return "-".join(m.groups())
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?[\s/-]+([A-Za-z]{3,9})\.?,?[\s/-]+(\d{4})", s)  # 17 Sep 2026, 28-Jul-2026
    if m and m.group(2)[:3].lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS.index(m.group(2)[:3].lower()) + 1:02}-{int(m.group(1)):02}"
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", s)
    if m and m.group(1)[:3].lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS.index(m.group(1)[:3].lower()) + 1:02}-{int(m.group(2)):02}"
    return s[:40]


def value_of(node, field, base_url):
    t = field.get("type", "text")
    if node.el is None:  # metadata
        vals = node.value or []
        if t in ("people", "list"):
            if len(vals) == 1:  # "Qiang Zhu (Sunnyvale, CA), John Chao (Foster City, CA)": split outside brackets
                vals = re.split(r"\s*;\s*|,\s*(?![^()]*\))", vals[0])
            return list(dict.fromkeys(v.strip() for v in vals if v.strip()))[:20]
        v = vals[0] if vals else ""
        return to_iso(v) if t == "date" else urljoin(base_url, v) if t == "url" else v[:1000]
    el = node.el
    if t == "url":
        links = [el] if el.name == "a" else el.find_all("a", href=True)
        links = [a for a in links if a.get("href") and not a["href"].startswith(("#", "javascript", "mailto"))]
        pdf = [a for a in links if re.search(r"\.pdf\b|download", a["href"], re.I)]
        return urljoin(base_url, (pdf or links)[0]["href"]) if links else ""
    if t == "date":
        tm = el if el.name == "time" else el.find("time")
        return to_iso(tm.get("datetime") or tm.get_text()) if tm is not None and (tm.get("datetime") or tm.get_text()) else to_iso(node.text)
    text = node.text
    # "Patent number: 10,929,987" in one element: drop the label when it is its own <strong>/<b>/<dt>/<span>
    first = el.find(["strong", "b", "dt", "span", "label", "th"])
    if first is not None and first is not el:
        lab = clean(first.get_text(" "))
        if 0 < len(lab) <= 40 and text.startswith(lab) and len(text) > len(lab) + 1:
            text = text[len(lab):].lstrip(" :–-")
    if t in ("people", "list"):
        parts = [clean(x.get_text(" ")) for x in el.find_all(["a", "li"])] or re.split(r"\s*(?:[,;|•]| and )\s*", text)
        return list(dict.fromkeys(p for p in parts if 1 < len(p) < 120))[:20]
    return text[:1000]


def fits(node, field, url, strict=True):
    """Does this candidate have the right shape for the field? Types always hold (a date parses, a URL is a
    link). strict adds the guesswork filters for laya's own walk: the plan's format pattern, words-not-codes,
    length close to the example. Values the LLM picked and graded skip those: sites format IDs their own way."""
    v = value_of(node, field, url)
    t = field.get("type", "text")
    if strict and field.get("pattern"):  # the plan knows the format (DOI, ISBN, patent no.): trust that over heuristics
        try:
            return any(re.search(field["pattern"], x) for x in (v if isinstance(v, list) else [str(v)]))
        except re.error:
            pass
    if t == "date":  # from a small element, a datetime attribute or metadata, not "first date in a big block"
        small = node.el is None or len(node.text) <= 80 or node.el.name == "time" or node.el.find("time") is not None
        if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", v or ""):
            return small
        # "Open - no closing date", "Rolling", "TBC": a real answer for a date field, kept as text when validated
        return not strict and small and 0 < len(v or "") <= 60
    if t == "url":  # a document link, not a logo or thumbnail
        return str(v).startswith("http") and not re.search(r"\.(png|jpe?g|gif|svg|webp|ico)(\?|$)", str(v), re.I)
    if t in ("number", "money"):  # a standalone number ("48 pages", "£2.5m"), not an ID that contains digits
        return bool(re.search(r"(?<![\w-])[£$€]?\d[\d,.]*(?![\w-])", str(v))) and len(str(v)) <= 60 and "://" not in str(v)
    if t == "people":  # names, possibly with an affiliation in brackets: "Walaa Eldin M. Moustafa (Mountain View, CA)"
        name = lambda x: re.sub(r"\s*\([^)]*\)", "", x)
        return bool(v) and len(node.text) < 1500 and all(len(name(x).split()) <= 6 and re.search(r"[^\W\d_]", x)
                                                         and not re.search(r"https?:|www\.|@", x) for x in v)
    if t == "list":  # tags read like words, not codes such as <D1480> or UUIDs
        return bool(v) and len(node.text) < 600 and all(len(x.split()) <= 8 and "://" not in x and
                                                        not re.search(r"[<>{}]", x)
                                                        and (not strict or re.search(r"[^\W\d_]{3,}", x)) for x in v)
    # free text: not a bare URL, a word count in the same league as the plan's example value, and a similar
    # makeup: an ID-like example (DOI, patent or grant number) wants digits, a prose example does not
    ex, v = str(field.get("example", "")), str(v)
    if not strict:
        return bool(v.strip())  # a license or homepage can be a bare URL; the grader judges the rest
    ew, cw = len(ex.split()) or 5, len(v.split())
    digits = lambda s: sum(c.isdigit() for c in s) / max(1, len(s))
    if digits(ex) >= 0.25 and digits(v) < 0.12 or digits(ex) == 0 and digits(v) > 0.5:
        return False
    return not re.match(r"https?://\S+$", v) and ew / 4 <= cw <= max(ew * 4, 12)


LLM_MAP = """We extract {what} records from item pages. Fields:
{fields}

Below are outlines of two item pages: every metadata value and every small element of the main content,
numbered, with its tag, its label if it has one, and the start of its text.
For each field, give the number of the element that holds exactly that field's value on each page, or null if
the page does not show it. Prefer the element that holds only the value (not a larger block and not the label
itself). Prefer a metadata entry when it is exactly the value.
{feedback}
{pages}

Return JSON: {{"<field name>": [<number on PAGE A or null>, <number on PAGE B or null>], ...}}"""

GRADE_PROMPT = """Below is an outline of a web page about one {what} (metadata values and page elements, numbered),
followed by the values a scraper extracted from it. For every field, judge the extracted value against the page:
- "correct": it is the right value (small formatting differences are fine)
- "wrong": it is not the right value for that field
- "missed": the field is empty but the page clearly shows it
- "absent": the field is empty and the page does not show it
Fields:
{fields}

PAGE ({url})
{page}

EXTRACTED
{record}

Return JSON: {{"<field name>": "correct|wrong|missed|absent", ...}}"""


LABEL_TAGS = ("strong", "b", "dt", "th", "label")


def kv_pairs(el):
    """<div><strong>Filed:</strong> Feb 18, 2016 <strong>Assignee:</strong> LinkedIn</div> -> [(label, value)]."""
    pairs, label, buf = [], None, []
    for c in el.children:
        if isinstance(c, Tag) and c.name in LABEL_TAGS and 0 < len(clean(c.get_text(" "))) <= 40:
            if label and clean(" ".join(buf)):
                pairs.append((label, clean(" ".join(buf)).lstrip(":– -")))
            label, buf = clean(c.get_text(" ")).rstrip(": "), []
        elif label:
            buf.append(c.get_text(" ") if isinstance(c, Tag) else str(c))
    if label and clean(" ".join(buf)):
        pairs.append((label, clean(" ".join(buf)).lstrip(":– -")))
    return pairs


def same_label(a, b):
    """"Inventors:" and "Inventor" are the same label: sites switch to the singular for one value."""
    norm_ = lambda s: re.sub(r"s$", "", clean(s).rstrip(": ").lower())
    return norm_(a) == norm_(b)


def kv_node(label, value, path):
    return Node(f"{label}: {value}", sel=f"kv:{path} || {label}", value=[value])


INLINE = {"i", "em", "sub", "sup", "u", "small", "abbr", "code", "figref", "math", "mi", "mo", "mn", "mrow", "msub",
          "msup", "mtext", "mfrac", "br", "wbr"}


def outline(tree, cap=280):
    """Numbered, compact view of a page for the LLM: metadata values, then small content elements."""
    root, soup, content = tree
    nodes = [n for g in root._kids or [] if g.el is None and g._kids for n in g._kids]
    seen, prose = set(), [0]

    def visit(el):
        for c in el.find_all(True, recursive=False):
            if len(nodes) >= cap:
                return
            n = el_node(c)
            if not n.text or id(n.el) in seen:
                continue
            seen.add(id(n.el))
            pairs = kv_pairs(n.el)
            if len(pairs) >= 2:  # a block of "Label: value" facts: each fact is a candidate of its own
                path = css_path(n.el, content)
                nodes.extend(kv_node(k, v, path) for k, v in pairs[:40])
                continue
            labelled = "labelled" in n.attrs
            # budget: body prose (a patent's description, an article) would crowd out the short labelled facts
            # that usually sit after it; bare numbers are figure/footnote markers unless labelled
            if len(n.text) > 150 and not labelled:
                prose[0] += 1
                keep = prose[0] <= 8
            else:
                keep = len(n.text) <= 300 and (labelled or not re.fullmatch(r"[\d\s.,]{1,6}", n.text))
            if keep and n.el.name not in INLINE:
                nodes.append(n)
            # prose paragraphs are one fact at most; their bold numerals, math and italics are not fields
            own = len(clean(" ".join(n.el.find_all(string=True, recursive=False))))
            if n.kids() and n.el.name != "p" and own < 100:
                visit(n.el)

    visit(content)
    def line(n):
        if n.sel.startswith("kv:"):
            return f"fact '{n.sel.split(' || ', 1)[1]}' = {n.value[0][:110]}"
        if n.el is None:
            return f"meta {n.sel.split(':', 1)[1]} = {n.text.split(': ', 1)[-1][:110]}"
        return f"{n.attrs[:90]} {n.text[:110]}"
    lines = [f"[{i}] {line(n)}" for i, n in enumerate(nodes)]
    return nodes, lines


def vkey(node, field, url):
    if field.get("type", "text") not in ("text", "people", "list"):
        return ""
    v = value_of(node, field, url)
    return clean("; ".join(v) if isinstance(v, list) else str(v)).lower()[:200]


class LLMUnavailable(RuntimeError):
    """The LLM could not be reached (rate limit, outage): selectors cannot be learned or validated."""


PATTERN_PROMPT = """We extract {what} records from item pages. These fields could not be tied to a single page element,
probably because the value is written inside the text (for example "Deadline: 28-Jul-2026 The council is ..."):
{fields}

Below is the main text of two item pages of the same site, one line per text block. For each field write ONE Python regular expression with
exactly one capture group that captures the value on both pages, and would on other pages of this site too.
Anchor it on nearby words the site uses consistently (labels such as "Deadline:", "Donor:", "Amount:"), be lenient
about spacing and case (you may start with (?i)), and end the capture before the next label or sentence.
Use null when the value is not in the text.
{feedback}
PAGE A ({url_a})
{text_a}

PAGE B ({url_b})
{text_b}

Return JSON: {{"<field name>": "<regex or null>", ...}}"""


def page_text(tree, limit=20000):
    """The main content as text, one line per text block (as on the page, so "Deadline: 9 October 2026" ends at
    the line break): what text patterns run over, and what the LLM saw when writing them."""
    lines = (clean(s) for s in tree[2].get_text("\n").splitlines())
    return "\n".join(s for s in lines if s)[:limit]


def passes(c):
    """A field's grading is good enough to trust its selector everywhere. Works for any number of graded pages
    ("uncertain" ones, where two gradings disagreed, do not count); on 4 pages: no wrong value and at most one
    miss, or at least 3 correct with at most one wrong and one miss."""
    n = c["correct"] + c["wrong"] + c["missed"] + c["absent"]
    if not c["correct"] or c["missed"] > n / 4:
        return False
    return not c["wrong"] or c["correct"] >= 0.75 * n and c["wrong"] <= n / 4


class Extractor:
    def __init__(self, brain, plan, beam=2, max_depth=10, use_llm=True):
        self.brain, self.plan, self.beam, self.max_depth, self.use_llm = brain, plan, beam, max_depth, use_llm
        self.what = plan.get("singular") or plan["target"]
        what = plan.get("singular") or plan["target"]
        self.q_in = {f["name"]: {"c": {"type": "noul", "instructions":
                     f"Does this part of a web page about one {what} contain its {f['name'].replace('_', ' ')} "
                     f"({f['description']})?"}} for f in plan["fields"]}
        self.q_is = {f["name"]: {"c": {"type": "noul", "instructions":
                     f"Is this exactly the {f['name'].replace('_', ' ')} of the {what} ({f['description']}), "
                     f"and not a heading, label, navigation or some other text?"}} for f in plan["fields"]}

    def _ask(self, rows):
        """rows: [(state, questions)] -> [p_yes]; one batched laya pass across fields and pages."""
        if not rows:
            return []
        res = self.brain.router.predict_batch([{"state": s, "questions": q} for s, q in rows], batch_size=64)
        return [r["answers"]["c"]["noul"] for r in res]

    def walk(self, jobs, taken=None):
        """jobs: [(root, field, url)] -> [(best node, confidence)], all walks advancing level by level together."""
        frontier = [[j[0]] for j in jobs]
        seen = [[] for _ in jobs]
        for _ in range(self.max_depth):
            rows, owner = [], []
            for i, (root, f, _) in enumerate(jobs):
                for n in frontier[i]:
                    for k in n.kids():
                        rows.append((k.state(), self.q_in[f["name"]]))
                        owner.append((i, k))
            if not rows:
                break
            scores = self._ask(rows)
            per = {}
            for (i, k), p in zip(owner, scores):
                per.setdefault(i, []).append((p, k))
            frontier = [[] for _ in jobs]
            for i, cands in per.items():
                cands.sort(key=lambda x: -x[0])
                keep = [c for c in cands[:self.beam] if c[0] >= 0.2] or cands[:1]
                seen[i] += keep
                # stop descending into small blocks: they are the answer or they are not
                frontier[i] = [k for _, k in keep if (k.el is not None or k._kids) and len(k.text) > 40 and k.kids()]
        rows, owner = [], []
        for i, (root, f, url) in enumerate(jobs):
            # the answer is often a child of the last block kept, so small children join the final round
            cands = {id(n): n for _, n in seen[i]}
            # metadata is small and high-value: every meta tag / JSON-LD value is a finalist
            for g in root._kids or []:
                for k in (g._kids if g.el is None and g._kids else []):
                    cands.setdefault(id(k), k)
            for _, n in seen[i][-2:]:
                for k in n.kids():
                    cands.setdefault(id(k), k)
            for n in cands.values():
                if fits(n, f, url):
                    rows.append((n.state(), self.q_is[f["name"]]))
                    owner.append((i, n, 0))
        ranked = [[] for _ in jobs]
        for (i, n, _), p_is in zip(owner, self._ask(rows)):
            ranked[i].append((p_is, n))
        return self.assign(jobs, [sorted(r, key=lambda x: -x[0])[:3] for r in ranked], taken)

    def assign(self, jobs, ranked, taken):
        """One element answers one field per page: the most confident claim wins, the other fields fall back
        to their next finalist. Dates and numbers are exempt (published and updated can be the same day)."""
        taken = taken if taken is not None else {}
        best, done = [(None, 0.0)] * len(jobs), set()
        for p, i, n in sorted(((p, i, n) for i, r in enumerate(ranked) for p, n in r), key=lambda x: -x[0]):
            if i in done:
                continue
            _, f, url = jobs[i]
            key = vkey(n, f, url)
            if key and key in taken.setdefault(url, set()):
                continue
            best[i] = (n, p)
            done.add(i)
            if key:
                taken[url].add(key)
        return best

    def selector(self, node, content):
        """CSS path, plus the element's label when it has one: in a table of "Inventors / Assignee / Filed"
        rows every value cell has the same path, and the label says which row is meant."""
        if node.sel:
            return node.sel
        label = label_of(node.el)
        return css_path(node.el, content) + (f" || {label}" if label else "")

    def find(self, sel, tree):
        root, soup, content = tree
        if sel.startswith("re:"):  # a text pattern over the main content; group 1 is the value
            try:
                m = re.search(sel[3:], page_text(tree))
            except re.error:
                return None
            v = clean(m.group(1) if m and m.groups() else m.group(0) if m else "")[:500]
            return Node(v, sel=sel, value=[v]) if v else None
        if sel.startswith("kv:"):
            path, _, label = sel[3:].partition(" || ")
            try:
                blocks = content.select(path)
            except Exception:
                return None
            return next((kv_node(k, v, path) for b in blocks for k, v in kv_pairs(b) if same_label(k, label)), None)
        if sel.startswith(("meta:", "ld:")):
            meta = next((k for k in root._kids if k.el is None and k._kids), None)
            return next((n for n in (meta._kids if meta else []) if n.sel == sel), None)
        path, _, label = sel.partition(" || ")
        try:
            hits = content.select(path) if path else []
        except Exception:
            return None
        el = next((h for h in hits if same_label(label_of(h), label)), None) if label else (hits[0] if hits else None)
        return el_node(el) if el is not None and clean(el.get_text(" ")) else None

    def llm_selectors(self, trees, sample, fields, log, feedback=""):
        """Show the LLM two pages as numbered outlines; it maps each field to an element -> {field: selector}."""
        from llm import chat_json
        outlines = [outline(trees[u]) for u in sample]
        pages_txt = "\n\n".join(f"PAGE {'AB'[i]} ({u})\n" + "\n".join(lines)
                                for i, (u, (nodes, lines)) in enumerate(zip(sample, outlines)))
        prompt = LLM_MAP.format(what=self.what, pages=pages_txt, feedback=feedback,
                                fields="\n".join(f"- {f['name']} ({f['type']}): {f['description']}" for f in fields))
        try:
            answer = chat_json(prompt)
        except Exception as e:
            if not feedback:  # the first mapping is not optional: without it there is nothing to validate
                raise LLMUnavailable(f"field mapping failed: {e}") from e
            log(f"  LLM repair failed ({e})")
            return {}
        learned = {}
        for f in fields:
            picks = answer.get(f["name"]) or []
            sels = []
            for (nodes, _), u, k in zip(outlines, sample, picks if isinstance(picks, list) else [picks]):
                if isinstance(k, int) and 0 <= k < len(nodes) and fits(nodes[k], f, u, strict=False):
                    sels.append(self.selector(nodes[k], trees[u][2]))
            if sels:
                learned[f["name"]] = Counter(sels).most_common(1)[0][0]
        return learned

    def pick(self, sels, f, tree, url):
        """The first of a field's selectors (layout variants) that yields a value of the right shape."""
        for sel in sels.get(f["name"], []):
            n = self.find(sel, tree)
            if n is not None and fits(n, f, url, strict=False):
                return n
        return None

    def record(self, sels, tree, url):
        """Apply selectors to one page -> {field: value}; nothing of the right shape gives ''."""
        rec = {}
        for f in self.plan["fields"]:
            n = self.pick(sels, f, tree, url)
            rec[f["name"]] = value_of(n, f, url) if n is not None and fits(n, f, url, strict=False) else \
                ([] if f.get("type") in ("people", "list") else "")
        return rec

    def grade(self, sels, trees, sample):
        """The LLM judges the selectors' values on each sample page -> {field: Counter(verdicts)}."""
        from llm import chat_json
        fields = self.plan["fields"]

        def one(u):
            rec = self.record(sels, trees[u], u)
            try:
                return chat_json(GRADE_PROMPT.format(
                    what=self.what, url=u, page="\n".join(outline(trees[u], cap=220)[1]),
                    fields="\n".join(f"- {f['name']}: {f['description']}" for f in fields),
                    record="\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)[:300]}" for k, v in rec.items()))), rec
            except Exception:
                return {}, rec

        results = [one(u) for u in sample]  # one call at a time: gentle on a shared plan
        if not any(verdict for verdict, _ in results):
            raise LLMUnavailable("every grading call failed")
        tally = {f["name"]: Counter() for f in fields}
        examples = {f["name"]: [] for f in fields}
        per = {}
        for u, (verdict, rec) in zip(sample, results):
            per[u] = {}
            for f in fields:
                v = str(verdict.get(f["name"], "?")).lower()
                per[u][f["name"]] = v
                tally[f["name"]][v] += 1
                if v in ("wrong", "missed"):
                    examples[f["name"]].append((u, v, rec[f["name"]]))
        return tally, examples, per

    def second_opinion(self, sels, trees, sample, tally, examples, per, log):
        """Grading is an LLM call and not perfectly repeatable. Where a field's verdicts are mixed (right on
        some pages, wrong or missed on others), the pages behind the negative verdicts are graded again; a
        verdict stands only where both gradings agree, disagreements count as "uncertain"."""
        mixed = [n for n, c in tally.items() if c["correct"] and (c["wrong"] or c["missed"])]
        pages = [u for u in sample if any(per[u].get(n) in ("wrong", "missed") for n in mixed)]
        if not pages:
            return
        _, _, again = self.grade(sels, trees, pages)
        flipped = []
        for n in mixed:
            c = Counter()
            for u in sample:
                v1 = per[u].get(n, "?")
                v2 = again.get(u, {}).get(n, v1) if u in again else v1
                c[v1 if v1 == v2 else "uncertain"] += 1
            if c != tally[n]:
                flipped.append(n)
            tally[n] = c
            examples[n] = [(u, v, val) for u, v, val in examples[n] if again.get(u, {}).get(n, v) == v]
        if flipped:
            log(f"    second opinion changed: {', '.join(flipped)}")

    def run(self, pages, item_pattern="", samples=4, rounds=2, guess=False, known=None, seed=None, log=print):
        """pages: {url: html} -> ({url: record}, report).

        1. the LLM maps fields to elements on two pages -> selectors
        2. the selectors run on `samples` pages (two of them unseen) and the LLM grades every value
        3. failing fields go back to the LLM with what went wrong, up to `rounds` times
        4. only fields whose selectors pass produce values; each value also gets laya's confidence
        A field that never passes stays empty ("unreliable"): a blank is honest, a wrong value is not.
        `guess` fills those with laya's own DOM walk instead, marked as guesses.
        `known`: a previous run's report ({"selectors", "fields"}) for this site; skips steps 1-3, no LLM.
        `seed`: selectors from a previous run to re-check (revalidation): they are graded first, only fields
        without one are mapped, and only failing fields are repaired."""
        fields = self.plan["fields"]
        trees = {u: page_tree(h, u, item_pattern) for u, h in pages.items()}
        urls = list(trees)
        sample = urls[:samples]
        if known:
            sels, status = {k: list(v) for k, v in known["selectors"].items()}, dict(known["fields"])
            log(f"  reusing {len(sels)} validated selectors (no LLM calls); --revalidate to check them again")
        else:
            sels = {k: list(v) for k, v in (seed or {}).items()}
            missing = [f for f in fields if f["name"] not in sels]
            if seed:
                log(f"  re-checking {len(sels)} saved selectors; mapping {len(missing)} fields without one")
            if missing and len(urls) >= 2:
                sels.update({k: [v] for k, v in self.llm_selectors(trees, urls[:2], missing, log).items()})
            status = {}
        for rnd in range(0 if known else rounds + 1):
            tally, examples, per = self.grade(sels, trees, sample)
            self.second_opinion(sels, trees, sample, tally, examples, per, log)
            failing = []
            for f in fields:
                c, name = tally[f["name"]], f["name"]
                if passes(c):
                    status[name] = f"validated ({c['correct']}/{len(sample)} correct)"
                elif c["absent"] and not (c["correct"] or c["wrong"] or c["missed"]):
                    status[name] = "absent (not shown on sample pages)"
                    sels.pop(name, None)
                else:
                    status[name] = f"unreliable ({dict(c)})"
                    failing.append(f)
            log(f"  round {rnd}: " + ", ".join(f"{n} {'ok' if s.startswith('validated') else 'absent' if s.startswith('absent') else 'FAIL'}"
                                            for n, s in status.items()))
            if not failing or rnd == rounds:
                break
            # repair: show the LLM the pages where these fields failed, and what it picked there
            bad_pages = [u for u, _ in Counter(u for f in failing for u, _, _ in examples[f["name"]]).most_common(2)]
            bad_pages += [u for u in sample if u not in bad_pages][:2 - len(bad_pages)]
            notes = "\n".join(f"- {f['name']}: on {u} the previous choice gave {json.dumps(v, ensure_ascii=False)[:120]}, "
                              f"judged {verdict}" for f in failing for u, verdict, v in examples[f["name"]][:2])
            feedback = ("\nA previous attempt got these fields wrong. Look again carefully, including labelled facts, "
                        f"tables and metadata; answer null only if the page truly does not show the field:\n{notes}\n")
            # a selector that gave wrong values is replaced; one that was right but missed pages gets a fallback
            for name, new in self.llm_selectors(trees, bad_pages, failing, log, feedback).items():
                old = sels.get(name, [])
                sels[name] = [new] if tally[name]["wrong"] or not old else old + [s for s in [new] if s not in old]
        if not known:  # fields that failed as elements may live inside the prose: try text patterns
            prose = [f for f in fields if status.get(f["name"], "").startswith("unreliable")]
            if prose:
                self.text_patterns(prose, sels, status, trees, sample, log)
        for f in fields:
            if not status.get(f["name"], "").startswith("validated"):
                sels.pop(f["name"], None)
            log(f"    {f['name']:20} {status[f['name']]:40} {' | '.join(s[-50:] for s in sels.get(f['name'], []))}")

        checked = dict((known or {}).get("coverage_checked", {}))
        changed = self.coverage(sels, trees, urls, sample, log, checked)
        out = {}
        for u in urls:
            rec = self.record(sels, trees[u], u)
            out[u] = {**rec, "_confidence": {}, "_how": {k: "selector" if (v or v == 0) and v != [] else "" for k, v in rec.items()}}
        # laya: one confidence per value, a per-item signal (not a gate)
        rows, owner = [], []
        for u in urls:
            for f in fields:
                n = self.pick(sels, f, trees[u], u)
                if n is not None and out[u][f["name"]] not in ("", []):
                    rows.append((n.state(), self.q_is[f["name"]]))
                    owner.append((u, f["name"]))
        for (u, name), p in zip(owner, self._ask(rows)):
            out[u]["_confidence"][name] = round(p, 3)
        if guess:
            todo = [(u, f) for u in urls for f in fields if f["name"] not in sels]
            log(f"  --guess: laya walk for {len(todo)} values of unvalidated fields ...")
            for (u, f), (n, p) in zip(todo, self.walk([(trees[u][0], f, u) for u, f in todo])):
                if n is not None and p >= 0.5:
                    out[u][f["name"]], out[u]["_confidence"][f["name"]], out[u]["_how"][f["name"]] = value_of(n, f, u), round(p, 3), "guess"
        return out, {"selectors": sels, "fields": status, "changed": changed, "coverage_checked": checked}

    def llm_patterns(self, trees, pages, fields, log, feedback=""):
        """The LLM writes one text pattern per field from two pages' main text -> {field: "re:<pattern>"}."""
        from llm import chat_json
        a, b = (pages * 2)[:2]
        try:
            answer = chat_json(PATTERN_PROMPT.format(
                what=self.what, feedback=feedback, url_a=a, url_b=b,
                text_a=page_text(trees[a], 5000), text_b=page_text(trees[b], 5000),
                fields="\n".join(f"- {f['name']} ({f['type']}): {f['description']}. Example: {f.get('example', '')}"
                                 for f in fields)))
        except Exception as e:
            log(f"  LLM text patterns failed ({e})")
            return {}
        out = {}
        for f in fields:
            pat = answer.get(f["name"])
            try:
                if isinstance(pat, str) and re.compile(pat).groups >= 1:
                    out[f["name"]] = "re:" + pat
            except re.error:
                pass
        return out

    def text_patterns(self, prose, sels, status, trees, sample, log, rounds=1):
        """Fields that failed as page elements get a text pattern, graded on the samples like any selector,
        with `rounds` repair attempts. Only patterns that pass are kept."""
        log(f"  text patterns for {', '.join(f['name'] for f in prose)} ...")
        feedback, pages = "", sample[:2]
        for rnd in range(rounds + 1):
            trial = self.llm_patterns(trees, pages, prose, log, feedback)
            if not trial:
                return
            tally, examples, _ = self.grade({**sels, **{k: [v] for k, v in trial.items()}}, trees, sample)
            still, notes = [], []
            for f in prose:
                name = f["name"]
                if name in trial and passes(tally[name]):
                    sels[name] = [trial[name]]
                    status[name] = f"validated as text pattern ({tally[name]['correct']}/{len(sample)} correct)"
                    log(f"    {name:20} text pattern ok: {trial[name][3:][:70]}")
                else:
                    still.append(f)
                    notes += [f"- {name}: pattern {trial.get(name, 'null')[3:][:80]!r} gave "
                              f"{json.dumps(v, ensure_ascii=False)[:80]} on {u}, judged {verdict}"
                              for u, verdict, v in examples[name][:2]]
            prose = still
            if not prose:
                return
            bad = [u for u, _ in Counter(u for f in prose for u, _, _ in examples[f["name"]]).most_common(2)]
            pages = (bad + [u for u in sample if u not in bad])[:2]
            feedback = "\nA previous attempt failed on these fields:\n" + "\n".join(notes) + "\n"
        for f in prose:
            log(f"    {f['name']:20} no reliable text pattern either")

    def coverage(self, sels, trees, urls, sample, log, checked, min_share=0.8, recheck_days=30):
        """A field validated on the samples but empty on many other pages usually has a second form there
        ("Total fund:" where the samples said "Maximum award:"). For each such field: show the LLM two pages where
        it came out empty, and keep the new selector only if those pages then grade correct. At most three
        paced calls per weak field, none when coverage is fine. Returns whether selectors changed."""
        changed = False
        for f in self.plan["fields"]:
            if f["name"] not in sels:
                continue
            empty = [u for u in urls if self.pick(sels, f, trees[u], u) is None]
            if len(empty) <= (1 - min_share) * len(urls):
                continue
            last = checked.get(f["name"])  # already looked recently: the answer was "no other form"
            if last and time.time() - time.mktime(time.strptime(last, "%Y-%m-%d")) < recheck_days * 86400:
                continue
            checked[f["name"]] = time.strftime("%Y-%m-%d")
            changed = True  # remember the check itself, so the next run does not repeat it
            probe = [u for u in empty if u not in sample][:2] or empty[:2]
            log(f"  coverage: {f['name']} empty on {len(empty)}/{len(urls)} pages; looking for another form on 2 of them")
            feedback = ("\nThe current selector finds nothing for this field on these pages. If the page shows the field "
                        "(perhaps under a different label or in a different place), give that element; otherwise null.\n")
            new = self.llm_selectors(trees, probe, [f], log, feedback).get(f["name"])
            if not new or new in sels[f["name"]]:
                log(f"    no other form found (the field may simply be absent there)")
                continue
            try:
                c = self.grade({f["name"]: sels[f["name"]] + [new]}, trees, probe)[0][f["name"]]
            except LLMUnavailable:
                log("    LLM unavailable; coverage check skipped")
                break
            if c["correct"] and not c["wrong"]:
                sels[f["name"]].append(new)
                changed = True
                log(f"    added fallback {new[-60:]}  ({c['correct']}/{len(probe)} correct)")
            else:
                log(f"    rejected {new[-60:]}  ({dict(c)})")
        return changed
