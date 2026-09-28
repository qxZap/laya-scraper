# Structure checks, no network / no model: python test_parse.py
from scrape import kids, norm, parent, parse

NAV = '<nav>' + ''.join(f'<a href="/en/{s}/">{s}</a>' for s in ["about", "events", "publications", "careers", "contact"]) + '</nav>'
LIST = NAV + '<main>' + ''.join(f'<a href="/en/publications/report-{i}/">A long report title number {i}</a>' for i in range(12)) + '<a rel="next" href="?page=2">next</a></main>'
ITEM = NAV + '<main><h1>One report</h1><a href="/en/publications/">Back to publications</a><a href="/files/r.pdf">PDF</a></main>'

p = parse("https://ex.org/en/publications/", LIST)
assert kids(p) == 12 and p["paged"], p
q = parse("https://ex.org/en/publications/report-1/", ITEM)
assert kids(q) == 0 and q["pdfs"] == 1, q
h = parse("https://ex.org/en/", NAV)
assert kids(h) == 0  # nav links are site chrome, not listed items
assert parent("/2025/12/some-paper") == parent("/2026/01/other/") == "/*/*/"
d = parse("https://ex.org/research/", NAV + "<main>" + "".join(f'<a href="/2025/{m:02}/paper-{m}">Research paper with a long title {m}</a>' for m in range(1, 9)) + "</main>")
assert kids(d) == 8  # items outside the page's own path, split across dated folders, still one list
assert norm("https://EX.org/en/publications?page=2#x") == "https://ex.org/en/publications"
from scrape import host
assert host("https://www2.fundsforngos.org/x") == host("https://www.fundsforngos.org/") == "fundsforngos.org"
assert host("https://catalog.data.gov/dataset") == host("https://data.gov") == "data.gov"
assert host("https://www.gov.uk/search") == "www.gov.uk" != host("https://www.bbc.co.uk/")  # separate sites under gov.uk / co.uk
assert host("https://www.bbc.co.uk/news") == host("https://bbc.co.uk") == "bbc.co.uk"
print("ok")
