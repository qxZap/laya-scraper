# list.py logic, no network / no model: python test_list.py
from list import facts, find_next, item_group, page_count, probes
from scrape import parse

base = "https://ex.org/research/"
PAGED = ('<main>' + ''.join(f'<a href="/2025/{m:02}/a-long-paper-slug-{m}">A research paper with a long title {m}</a>' for m in range(1, 11))
         + ''.join(f'<a href="/search/authors_for_facets/{i}/type/x">Some Author Name Facet {i}</a>' for i in range(12))
         + '<nav class="pager"><a href="?page=0">1</a><a href="?page=1">Page 2</a><a href="?page=36">Page 37</a></nav></main>')

# the publications win over a bigger group of facet links
gpath, items = item_group(parse(base, PAGED))
assert gpath == "/*/*/" and len(items) == 10, gpath

# numbered link to page 2 (0-based ?page=1), and the last page seen in the pager
nxt = find_next(base, PAGED, 1)
assert nxt == base + "?page=1", nxt
assert page_count(nxt, PAGED, base) == 37  # ?page=36 is the 37th page when counting from 0
assert find_next(base, '<a rel="next" href="/research/p/2">x</a>', 1) == "https://ex.org/research/p/2"
for label in ("Next", "Next ›", "Next page →", "»", "Load more"):
    assert find_next(base, f'<a href="?pg=2">{label}</a>', 1) == base + "?pg=2", label
assert find_next(base, '<a href="/about/">About</a><a href="/x/"><img src="i.png"></a>', 1) is None  # icon-only link is not "next"
assert find_next(base, "<p>no pager</p>", 1) is None
assert ("?page={}", base + "?page=2", 0) in list(probes(base, 2))

ITEM = """<html><head>
<meta name="citation_title" content="Water footprints of trade">
<meta name="citation_author" content="Morris, Leslie"><meta name="citation_author" content="Nxumalo, Phesheya">
<meta name="citation_publication_date" content="2026/05/13"><meta name="citation_pdf_url" content="https://ex.org/f/w.pdf">
<meta name="description" content="How trade moves water.">
<script type="application/ld+json">{"@graph":[{"@type":"Report","keywords":["water","trade"]}]}</script>
</head><body><h1>ignored</h1><a href="https://doi.org/10.1234/abc.5">doi</a></body></html>"""
f = facts("https://ex.org/2026/05/water", ITEM)
assert f["title"] == "Water footprints of trade" and f["date"] == "2026/05/13"
assert f["authors"] == ["Morris, Leslie", "Nxumalo, Phesheya"] and f["pdf"].endswith("w.pdf")
assert f["doi"] == "10.1234/abc.5" and f["topics"] == ["water", "trade"] and f["schema_type"] == "Report"

# no metadata: fall back to the page, and keep social handles / "See more" out of the authors
BARE = """<h1>Demand-driven inflation</h1><time datetime="2026-09-23T10:00:00">Sep 23</time>
<div class="authors"><a>See More</a><a>Domenico Giannone</a><a>@domenicogiannon</a><a>+1 more</a></div>"""
g = facts("https://ex.org/articles/demand", BARE)
assert g["title"] == "Demand-driven inflation" and g["date"] == "2026-09-23" and g["authors"] == ["Domenico Giannone"], g
print("ok")
