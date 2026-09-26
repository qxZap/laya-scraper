// Real-browser renderer (puppeteer-real-browser, passes Cloudflare). One browser, up to TABS pages at once.
// stdin: one URL per line, or a JSON line {"url", "more": N} to expand a lazy list N times first.
// stdout: one JSON line per URL, in completion order: {url, final_url, status, html, more} or {url, error}.
// Tabs share cookies, so a solved challenge sticks.
const { connect } = require('puppeteer-real-browser');
const readline = require('readline');

const TABS = +process.env.TABS || 4;
// we only read the DOM. CSS is skipped too, except on lists being expanded: lazy lists and "load more" need layout
const SKIP_TYPES = new Set(['image', 'media', 'font']);
const CHALLENGE = /just a moment|attention required|checking your browser|verify you are human/i;
const idle = page => page.waitForNetworkIdle({ idleTime: 500, timeout: 6000 }).catch(() => {});

// Grow a lazy list: scroll to the bottom and click a visible in-page "load/show more" control, repeatedly,
// until the page stops gaining links. Covers infinite scroll and load-more buttons alike.
async function expand(page, times) {
  const done = { rounds: 0, clicks: 0 };
  await page.bringToFront(); // lazy lists load on visibility; background tabs never see it
  await page.evaluate(() => document.querySelector('a[href]')?.scrollIntoView());
  // async lists arrive after "network idle"; wait until the link count holds still (max ~8s)
  for (let i = 0, last = -1, same = 0; i < 16 && same < 2; i++) {
    const n = await page.evaluate(() => document.querySelectorAll('a[href]').length);
    same = n === last ? same + 1 : 0;
    last = n;
    await new Promise(r => setTimeout(r, 500));
  }
  for (let i = 0; i < times; i++) {
    const before = await page.evaluate(() => document.querySelectorAll('a[href]').length);
    const clicked = await page.evaluate(() => {
      const re = /^(load|show|view|see)\s+more\b|^more (results|items|publications|articles)\b|^load\b/i;
      const el = [...document.querySelectorAll('button, [role=button], a[href="#"], a[href^="javascript"], a:not([href])')]
        .find(e => re.test((e.innerText || '').trim()) && e.offsetParent !== null);
      if (el) el.click();
      return !!el;
    });
    await page.bringToFront();
    await page.evaluate(() => window.scrollTo(0, (document.scrollingElement || document.documentElement).scrollHeight));
    await idle(page);
    const after = await page.evaluate(() => document.querySelectorAll('a[href]').length);
    if (after <= before) break;
    done.rounds++;
    done.clicks += clicked;
  }
  return done;
}

(async () => {
  // ponytail: true headless is fingerprinted by Cloudflare; a headed window parked off-screen is
  // invisible and passes. HEADLESS=1 forces real headless for sites without a challenge.
  const { browser } = await connect({
    headless: process.env.HEADLESS === '1',
    turnstile: true,
    // background tabs get throttled and their challenges stall; keep every tab running at full speed
    args: ['--window-position=-32000,-32000', '--disable-background-timer-throttling',
           '--disable-backgrounding-occluded-windows', '--disable-renderer-backgrounding'],
  });
  // Chrome only fully renders the front tab of a window, so lazy lists in background tabs never load.
  // Each "tab" is therefore its own off-screen window: all render, and they still share cookies.
  const cdp = await browser.target().createCDPSession();
  async function newWindow(keepCss) {
    const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank', newWindow: true });
    const { windowId } = await cdp.send('Browser.getWindowForTarget', { targetId });
    await cdp.send('Browser.setWindowBounds', { windowId, bounds: { left: -32000, top: -32000, width: 1280, height: 900 } });
    const target = await browser.waitForTarget(t => t._targetId === targetId);
    const page = await target.page();
    if (process.env.LOAD_ALL !== '1') {
      // challenge resources always load, or the check can fail
      await page.setRequestInterception(true);
      page.on('request', r => {
        if (r.isInterceptResolutionHandled()) return;
        const t = r.resourceType();
        const skip = (SKIP_TYPES.has(t) || t === 'stylesheet' && !keepCss) && !/challenges\.cloudflare\.com|\/cdn-cgi\//.test(r.url());
        (skip ? r.abort() : r.continue()).catch(() => {});
      });
    }
    return page;
  }
  let active = 0;
  const queue = [];

  async function render(job, retry = 1) {
    const r = await renderOnce(job);
    // a challenge redirect can land mid-read ("Execution context was destroyed"): read it again
    if (retry && /context was destroyed|detached|navigat/i.test(r.error || '')) return render(job, retry - 1);
    return r;
  }

  async function renderOnce({ url, more = 0 }) {
    const page = await newWindow(more > 0);
    try {
      let res, challenged = false;
      // mid-challenge the page navigates under us; "can't read it right now" means "still challenging"
      const blocked = async () => CHALLENGE.test(await page.title().catch(() => 'just a moment'));
      // intermittently a challenge doesn't clear; a fresh load (with the cookies earned so far) usually does
      for (let attempt = 0; attempt < 3 && (!res || await blocked()); attempt++) {
        res = await page.goto(url, { waitUntil: 'networkidle2', timeout: 45000 }).catch(e => {
          if (!/context was destroyed|navigat|detached/i.test(String(e))) throw e;
          return null;
        }) || res;
        // the challenge can clear inside goto; its 403 is flagged by Cloudflare, not a real error
        if (res && res.headers()['cf-mitigated']) challenged = true;
        for (let i = 0; i < 16 && await blocked(); i++) {
          challenged = true;
          if (i % 4 === 0) await page.bringToFront().catch(() => {}); // challenge widgets want a focused tab
          res = (await page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 1500 }).catch(() => null)) || res;
        }
      }
      await page.waitForNetworkIdle({ idleTime: 500, timeout: 8000 }).catch(() => {});
      if (challenged && !await blocked()) res = null; // passed; the 403 was the challenge itself
      // lazy lists often render on scroll
      await page.evaluate(() => window.scrollTo(0, (document.scrollingElement || document.documentElement).scrollHeight)).catch(() => {});
      await new Promise(r => setTimeout(r, 800));
      const grown = more ? await expand(page, more) : null;
      const status = await blocked() ? 403 : res ? res.status() : 200;
      return { url, final_url: page.url(), status, title: await page.title(), html: await page.content(), more: grown };
    } catch (e) {
      return { url, error: String(e) };
    } finally {
      await page.close().catch(() => {});
    }
  }

  function pump() {
    while (active < TABS && queue.length) {
      active++;
      render(queue.shift()).then(r => {
        process.stdout.write(JSON.stringify(r) + '\n');
        active--;
        pump();
      });
    }
  }

  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', line => {
    line = line.trim();
    if (!line) return;
    queue.push(line.startsWith('{') ? JSON.parse(line) : { url: line });
    pump();
  });
  rl.on('close', async () => {
    while (active || queue.length) await new Promise(r => setTimeout(r, 200));
    await browser.close();
    process.exit(0);
  });
})();
