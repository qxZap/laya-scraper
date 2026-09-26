# extract.py logic, no network / no model: python test_extract.py
from extract import css_path, fits, page_tree, to_iso, value_of

assert to_iso("17 September 2026") == "2026-09-17" == to_iso("Sep 17, 2026") == to_iso("2026/09/17") == to_iso("2026-09-17T10:00")

HTML = """<html><head>
<meta name="citation_title" content="Water footprints of trade"><meta name="citation_author" content="Leslie Morris">
<meta name="citation_author" content="Phesheya Nxumalo"><meta name="viewport" content="width=device-width">
<script type="application/ld+json">{"@type":"Report","datePublished":"2026-05-13"}</script></head><body>
<header><nav><a href="/about">About</a></nav></header>
<main><article><h1 class="title">Water footprints of trade</h1><time datetime="2026-05-13">13 May 2026</time>
<p class="lead">How international trade moves water between regions, and what that means for scarcity.</p>
<a href="/files/report.pdf">Download PDF</a>
<div class="related"><a href="/2026/04/other-paper">Another paper with a long title</a><time>01 April 2026</time></div>
</article></main><footer>© site</footer></body></html>"""
root, soup, content = page_tree(HTML, "https://ex.org/2026/05/water", item_pattern="/*/*/")
meta, main = root._kids
keys = {n.sel for n in meta._kids}
assert {"meta:citation_title", "meta:citation_author", "ld:datePublished"} <= keys and "meta:viewport" not in keys
assert "About" not in content.get_text() and "Another paper" not in content.get_text()  # chrome + related card gone
assert "01 April 2026" not in content.get_text()

authors = next(n for n in meta._kids if n.sel == "meta:citation_author")
assert value_of(authors, {"type": "people"}, "") == ["Leslie Morris", "Phesheya Nxumalo"]
h1 = next(k for k in main.kids() if k.el.name == "h1")
assert css_path(h1.el, content) == "article > h1.title"

T = lambda **kw: {"name": "f", "description": "", **kw}
node = lambda sel: next(k for k in main.kids() if k.el.name == sel)
assert fits(node("time"), T(type="date"), "") and not fits(h1, T(type="date"), "")
assert fits(node("a"), T(type="url"), "https://ex.org/") and value_of(node("a"), T(type="url"), "https://ex.org/") == "https://ex.org/files/report.pdf"
assert fits(node("p"), T(type="text", example="A two sentence summary of what the paper finds and argues."), "")
assert not fits(h1, T(type="text", example="10.1234/abcd.2024.001"), "")  # a title is not an ID
assert not fits(h1, T(type="text", pattern=r"10\.\d{4,9}/"), "")           # nor a DOI by pattern
assert not fits(authors, T(type="people", pattern=r"^\d+$"), "")

L = lambda vals, **kw: type("N", (), {"el": None, "value": vals, "text": "; ".join(vals)})()
assert not fits(L(["<D1480>"]), T(type="list"), "") and fits(L(["Transport", "Buses"]), T(type="list"), "")
assert not fits(L(["1327984f-95e0-4ca7-94c7-c63e69c30924"]), T(type="number"), "") and fits(L(["48 pages"]), T(type="number"), "")
assert not fits(L(["https://ex.org/logo.png"]), T(type="url"), "https://ex.org/")
root2, _, _ = page_tree('<script type="application/ld+json">{"@graph":[{"@type":"BreadcrumbList","itemListElement":[{"name":"Home"}]},{"@type":"Report","name":"X"}]}</script>')
assert {n.sel for n in root2._kids[0]._kids} == {"ld:@type", "ld:name"}  # breadcrumbs are not content

# "Label: value" blocks become facts with a selector that survives across pages
from extract import Extractor, kv_pairs, outline
from bs4 import BeautifulSoup
KV = ('<main><div class="facts"><strong>Patent number</strong>: 10387788 <strong>Filed:</strong> Feb 18, 2016 '
      '<strong>Inventors</strong>: Qiang Zhu (Sunnyvale, CA), John Chao (Foster City, CA)</div>'
      '<a href="/patent/20170243127">20170243127</a></main>')
assert kv_pairs(BeautifulSoup(KV, "html.parser").div) == [
    ("Patent number", "10387788"), ("Filed", "Feb 18, 2016"), ("Inventors", "Qiang Zhu (Sunnyvale, CA), John Chao (Foster City, CA)")]
tree = page_tree(KV, "https://ex.org/patent/10387788", item_pattern="/patent/")  # numeric reference link is no card
facts = {n.sel.split(" || ")[1]: n for n in outline(tree)[0] if n.sel.startswith("kv:")}
assert value_of(facts["Inventors"], T(type="people"), "") == ["Qiang Zhu (Sunnyvale, CA)", "John Chao (Foster City, CA)"]
assert value_of(facts["Filed"], T(type="date"), "") == "2016-02-18"
assert Extractor.find(None, facts["Patent number"].sel, tree).value == ["10387788"]
one = page_tree(KV.replace("Inventors", "Inventor").replace(", John Chao (Foster City, CA)", ""), "https://ex.org/patent/1", "/patent/")
assert Extractor.find(None, facts["Inventors"].sel, one).value == ["Qiang Zhu (Sunnyvale, CA)"]  # singular label

# validated path: formats are hints (the grader judges), dates may be "Open - no closing date"
pn = T(type="text", example="US11,234,567 B2", pattern=r"[A-Z]{2}\d")
assert not fits(facts["Patent number"], pn, "") and fits(facts["Patent number"], pn, "", strict=False)
rolling = type("N", (), {"el": None, "value": ["Open - no closing date"], "text": "Open - no closing date"})()
assert not fits(rolling, T(type="date"), "") and fits(rolling, T(type="date"), "", strict=False)

# a field keeps several selectors (layout variants); the first that yields a value wins
ex = Extractor.__new__(Extractor)
ex.plan = {"fields": [{"name": "award", "type": "money", "description": ""}]}
A = page_tree('<main><dl><dt>Maximum award:</dt><dd>£100,000</dd></dl></main>')
B = page_tree('<main><dl><dt>Total fund:</dt><dd>£2 million</dd></dl></main>')
sels = {"award": ["dl > dd || Maximum award:", "dl > dd || Total fund:"]}
assert ex.record(sels, A, "")["award"] == "£100,000" and ex.record(sels, B, "")["award"] == "£2 million"
print("ok")
