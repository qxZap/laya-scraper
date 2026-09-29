// Ads for our own apps (data in makers.js). showcase.css switches the layout:
// wide screens stack up to three cards in the right rail and, when there are more ads than that, slide the stack up
// one card at a time, so it always shows three however many there are; small screens show one ad at a time in a
// bar fixed to the bottom. Plain links, no tracking, no network: logos are local files and every animation is CSS.
const INTERVAL = 6000; // ms between moves; the timer's CSS animation runs this long, then the next ad comes in
const RAIL_CARDS = 3;
const RAIL = matchMedia('(min-width: 75rem)');
const REDUCE = matchMedia('(prefers-reduced-motion: reduce)');

const h = (tag, className, ...kids) => {
  const e = document.createElement(tag);
  if (className) e.className = className;
  e.append(...kids);
  return e;
};
const setVars = (e, vars, prefix = '') => {
  for (const [k, v] of Object.entries(vars)) e.style.setProperty(`--${prefix}${k}`, v);
  return e;
};

// One small animated scene per product, made of plain elements and animated by showcase.css while the card is showing.
const MOTIFS = {
  // scrape.land: fields of a page resolve into structured values while a scan beam passes
  data: () => h('div', 'sc-motif sc-motif-data',
    ...[['"title"', 0.9], ['"price"', 0.5], ['"in_stock"', 0.7], ['"rating"', 0.4]].map(([key, w], i) =>
      setVars(h('div', 'sc-row', h('span', 'sc-key', key), setVars(h('span', 'sc-val'), { w })), { i })),
    h('div', 'sc-beam')),
  // Penholder: the team's list on a phone. The pen holder drags the item that matters most above the line
  // and the rest slide down one place. Rows sit in 1.65rem slots, with a .7rem gap for the line after slot 2.
  order: () => {
    const Y = s => s * 1.65 + (s >= 2 ? 0.7 : 0);
    const rem = v => `${+v.toFixed(3)}rem`;
    // [text, destination slot]; the list starts in this order
    const rows = [['Pricing page', 1], ['Onboarding emails', 2], ['Quarterly report', 3], ['Fix checkout bug', 0]];
    const list = h('div', 'sc-list',
      ...[0, 1, 2, 3].map(s => setVars(h('span', 'sc-rank', String(s + 1)), { y: rem(Y(s)) })),
      setVars(h('span', 'sc-divider', 'Above the line'), { y: rem(Y(2) - 0.8) }),
      ...rows.map(([text, to], from) => setVars(
        h('div', to === 0 ? 'sc-card is-rising' : 'sc-card', text, ...(to === 0 ? [h('span', 'sc-finger')] : [])),
        { y: rem(Y(from)), dy: rem(Y(to) - Y(from)) })));
    return h('div', 'sc-motif sc-motif-order',
      h('div', 'sc-phone', h('div', 'sc-screen',
        h('div', 'sc-app-bar', h('span', null, 'Q3 priorities'), h('span', 'sc-pen')),
        list)));
  },
  // Censory: a scanned document with made-up personal data. Each value is detected, then blacked out.
  redact: () => h('div', 'sc-motif sc-motif-redact',
    h('div', 'sc-doc-title', 'Employment contract', h('span', null, 'Page 1')),
    // Made up on purpose: 123-45-6789 is not a valid SSN, 555-01xx numbers are reserved for fiction, example.com for examples.
    ...[['Name', 'Emily Johnson'], ['SSN', '123-45-6789'], ['Address', '1200 Oak St, Austin, TX'], ['Phone', '+1 (212) 555-0142'], ['Email', 'emily.j@example.com']]
      .map(([label, value], i) => h('div', 'sc-doc-row', h('span', 'sc-doc-label', label), setVars(h('span', 'sc-pii', value), { i })))),
  // Vetrosoft: a small business website puts itself together in a browser window, then the price lands on it.
  site: () => h('div', 'sc-motif sc-motif-site',
    h('div', 'sc-browser',
      h('div', 'sc-browser-bar', h('i'), h('i'), h('i'), h('span', 'sc-url', 'yourbusiness.com')),
      h('div', 'sc-site',
        setVars(h('div', 'sc-site-nav sc-build', h('b', null, 'Bakery'), h('i'), h('i'), h('i')), { i: 0 }),
        setVars(h('div', 'sc-site-hero sc-build', 'Fresh bread, baked every morning'), { i: 1 }),
        setVars(h('div', 'sc-site-btn sc-build', 'Order online'), { i: 2 }),
        setVars(h('div', 'sc-site-tiles sc-build', h('i'), h('i'), h('i')), { i: 3 }))),
    h('span', 'sc-price', 'From €29/mo')),
};

export function mountAds(slot, ads) {
  const track = h('div', 'showcase-track');
  const timer = h('div', 'showcase-timer', h('i'));

  for (const ad of ads) {
    const motif = MOTIFS[ad.motif]();
    const art = h('span', 'sc-art', motif);
    art.setAttribute('aria-hidden', 'true');
    const arrow = h('span', 'sc-arrow', '→');
    arrow.setAttribute('aria-hidden', 'true');
    const logo = Object.assign(document.createElement('img'), { src: ad.logo, alt: '', width: 40, height: 40, className: 'sc-logo' });
    const card = setVars(h('a', 'sc-slide',
      art,
      h('span', 'sc-brand', logo, h('span', 'sc-name', ad.title)),
      h('span', 'sc-headline', ad.headline),
      h('span', 'sc-cta', h('span', 'sc-cta-text', ad.cta), arrow)), ad.theme, 'sc-');
    // A new tab, so following an ad never throws away what someone was doing on this page.
    Object.assign(card, { href: ad.url, target: '_blank', rel: 'noopener' });
    track.append(card);
  }

  const unit = setVars(h('div', 'showcase', h('div', 'showcase-stage', track), timer),
    { 'sc-interval': `${INTERVAL}ms`, 'sc-rows': Math.min(ads.length, RAIL_CARDS) });

  // The first cards in the track are the ones showing (three in the rail, one in the bar): they play their scene and
  // can be tabbed to. The rest wait out of view. The timer only runs when there is something to rotate in.
  const settle = () => {
    const showing = RAIL.matches ? RAIL_CARDS : 1;
    [...track.children].forEach((card, i) => {
      card.classList.toggle('is-active', i < showing);
      card.inert = i >= showing;
    });
    timer.classList.remove('is-running');
    if (ads.length <= showing) return;
    void timer.offsetWidth; // restart the timer animation
    timer.classList.add('is-running');
  };
  // The top card goes to the back of the queue. In the bar the next ad fades in; in the rail the stack first slides up
  // by one card, with the next card already playing its scene as it comes into view.
  const finish = () => {
    track.classList.remove('is-sliding');
    track.append(track.firstElementChild);
    settle();
  };
  const advance = () => {
    if (!RAIL.matches || REDUCE.matches) return finish();
    track.children[RAIL_CARDS].classList.add('is-active');
    track.classList.add('is-sliding');
  };
  track.addEventListener('transitionend', e => { if (e.target === track && track.classList.contains('is-sliding')) finish(); });
  unit.addEventListener('animationend', e => { if (e.animationName === 'sc-progress') advance(); });
  RAIL.addEventListener('change', () => { track.classList.remove('is-sliding'); settle(); });

  // Pause while someone points at or tabs into the ads, and while the tab is hidden.
  const pause = on => unit.classList.toggle('is-paused', on);
  unit.addEventListener('pointerenter', () => pause(true));
  unit.addEventListener('pointerleave', () => pause(unit.contains(document.activeElement)));
  unit.addEventListener('focusin', () => pause(true));
  unit.addEventListener('focusout', e => pause(unit.contains(e.relatedTarget)));
  document.addEventListener('visibilitychange', () => unit.classList.toggle('is-hidden-tab', document.hidden));

  slot.replaceChildren(unit);
  document.body.classList.add('has-showcase-bar');
  settle();
}
