// Our other apps, shown by showcase.js. Plain links, no tracking beyond the utm_source in the URL.
// Logos are served from this site (img-src 'self'). A JS module because the CSP blocks fetching JSON,
// and named makers.js, not ads.js: ad blockers block files called ads.js, which would break the page's import.
// theme keys become --sc-<key> custom properties on each card; copy comes from each product's own site.
export default [
  {
    title: 'scrape.land',
    headline: 'The web, turned into data.',
    cta: 'Start free',
    url: 'https://scrape.land/?utm_source=laya-scraper',
    logo: 'makers/scrapeland.svg',
    motif: 'data',
    theme: { bg: 'linear-gradient(160deg, #131d30, #0b0f17 62%)', fg: '#e9f0fa', muted: '#a8b5c7', accent: '#3fdc7a', 'accent-2': '#54a8ff', 'cta-bg': '#3fdc7a', 'cta-ink': '#04140a', panel: 'rgba(255, 255, 255, .06)' },
  },
  {
    title: 'Penholder',
    headline: 'Everything is a priority. Put them in order.',
    cta: 'Try Penholder',
    url: 'https://penholder.app/?utm_source=laya-scraper',
    logo: 'makers/penholder.svg',
    motif: 'order',
    theme: { bg: 'linear-gradient(155deg, #4f46e5, #6a3fd6)', fg: '#ffffff', muted: '#e4e2ff', accent: '#ffffff', 'accent-2': '#c7d2fe', 'cta-bg': '#ffffff', 'cta-ink': '#3730a3', panel: 'rgba(255, 255, 255, .1)' },
  },
  {
    title: 'Censory',
    headline: 'Redact personal data from PDFs and scans.',
    cta: 'Try Censory',
    url: 'https://censory.app/?utm_source=laya-scraper',
    logo: 'makers/censory.svg',
    motif: 'redact',
    theme: { bg: '#111a26', fg: '#f4efe3', muted: '#c0c7d2', accent: '#c9a24a', 'accent-2': '#e3c37a', 'cta-bg': '#c9a24a', 'cta-ink': '#111a26', panel: '#f4efe3' },
  },
  {
    title: 'Vetrosoft',
    headline: 'The website your business is missing.',
    cta: 'Get a website',
    url: 'https://vetrosoft.com/?utm_source=laya-scraper',
    logo: 'makers/vetrosoft.svg',
    motif: 'site',
    theme: { bg: 'linear-gradient(160deg, #1c1917, #0c0a09 60%)', fg: '#faf6f1', muted: '#d6cfc4', accent: '#2b8cff', 'accent-2': '#63f0ff', 'cta-bg': 'linear-gradient(135deg, #63f0ff, #2b8cff 50%, #7d6cff)', 'cta-ink': '#0c0a09', panel: 'linear-gradient(160deg, #1f3b5c, #2a2350)' },
  },
];
