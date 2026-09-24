// Real-browser renderer (puppeteer-real-browser, passes Cloudflare). One browser, up to TABS pages at once.
// stdin: one URL per line. stdout: one JSON line per URL, in completion order:
// {url, final_url, status, html} or {url, error}. Tabs share cookies, so a solved challenge sticks.
const { connect } = require('puppeteer-real-browser');
const readline = require('readline');

const TABS = +process.env.TABS || 4;
const CHALLENGE = /just a moment|attention required|checking your browser|verify you are human/i;

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
  let active = 0;
  const queue = [];

  async function render(url) {
    const page = await browser.newPage();
    try {
      let res, challenged = false;
      // intermittently a challenge doesn't clear; a fresh load (with the cookies earned so far) usually does
      for (let attempt = 0; attempt < 3 && (!res || CHALLENGE.test(await page.title())); attempt++) {
        res = await page.goto(url, { waitUntil: 'networkidle2', timeout: 45000 });
        // the challenge can clear inside goto; its 403 is flagged by Cloudflare, not a real error
        if (res && res.headers()['cf-mitigated']) challenged = true;
        for (let i = 0; i < 12 && CHALLENGE.test(await page.title()); i++) {
          challenged = true;
          if (i % 4 === 0) await page.bringToFront(); // challenge widgets want a focused tab
          res = (await page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 1500 }).catch(() => null)) || res;
        }
      }
      if (challenged && !CHALLENGE.test(await page.title())) res = null; // passed; the 403 was the challenge itself
      // lazy lists often render on scroll
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      await new Promise(r => setTimeout(r, 800));
      const status = CHALLENGE.test(await page.title()) ? 403 : res ? res.status() : 200;
      return { url, final_url: page.url(), status, title: await page.title(), html: await page.content() };
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
  rl.on('line', url => { queue.push(url.trim()); pump(); });
  rl.on('close', async () => {
    while (active || queue.length) await new Promise(r => setTimeout(r, 200));
    await browser.close();
    process.exit(0);
  });
})();
