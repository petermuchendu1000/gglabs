// Drive the real dashboard in a real browser and screenshot every outcome.
// Clicks the shipped buttons; nothing is invoked directly except where noted.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const HERE = __dirname;
const SHOTS = process.env.E2E_SHOTS || path.join(HERE, 'shots');
const MODE = process.env.E2E_MODE_FILE || path.join(HERE, '.e2e_mode');
const BASE = process.env.E2E_BASE || 'http://127.0.0.1:5000';
const TOKEN = process.env.APP_AUTH_TOKEN;
if (!TOKEN) { console.error('APP_AUTH_TOKEN must be set'); process.exit(2); }
fs.mkdirSync(SHOTS, { recursive: true });

const LOG = [];
const say = (m) => { console.log(m); LOG.push(m); };
const setMode = (m) => { fs.writeFileSync(MODE, m); say(`    [mode] next Namecheap/CH outcome = ${m}`); };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function shot(page, name) {
  const p = path.join(SHOTS, name);
  await page.screenshot({ path: p });
  say(`    [shot] ${p}`);
}

async function addCompany(page, cn) {
  await page.click("button:has-text('+ Add Company')");
  await page.fill('#add-input', cn);
  await page.waitForSelector(`.sri[data-cn="${cn}"]`, { timeout: 30000 });
  await page.click(`.sri[data-cn="${cn}"]`);
  await page.waitForSelector(`.company-item:has-text("${cn}")`, { timeout: 30000 });
  say(`    added ${cn} through the Add dialog (real admission check + autofill)`);
}

async function selectCompany(page, cn) {
  await page.click(`.company-item:has-text("${cn}")`);
  await page.waitForSelector('#srow-domain', { timeout: 30000 });
}

async function runStage(page, cn, stage, expect, timeout = 120000) {
  const row = `#srow-${stage}`;
  await page.locator(`${row} .stage-btns button`).last().click();
  // Wait on the JOB, not on the row. _compute_stages marks `details` done from
  // the stored company name alone, so that row reads "Done" before the stage
  // has even started and waiting on it screenshots the drawer too early.
  await page.waitForFunction(async ([c, s]) => {
    const r = await fetch(`/api/v2/company/${c}/job/${s}`);
    const j = await r.json();
    return j.status === 'done' || j.status === 'failed';
  }, [cn, stage], { timeout, polling: 500 });
  await sleep(3200);          // let the page's own 2.5s poll refresh the drawer
  const status = (await page.locator(`${row} .stage-status-row`).innerText()).trim();
  const drawer = await page.locator('#log-pre').innerText();
  say(`    ${stage}: row says ${JSON.stringify(status)}`);
  for (const line of drawer.split('\n')) say(`      drawer | ${line}`);
  if (!status.toLowerCase().includes(expect.toLowerCase())) {
    throw new Error(`${stage}: expected ${expect}, row said ${status}`);
  }
  return { status, drawer };
}

(async () => {
  const results = {};
  // results is populated from the anonymous check onward
  const browser = await chromium.launch();
  // First, with no credentials at all: the gate must refuse. auth.py answers
  // a browser with a redirect to /login rather than a 401 challenge, which is
  // also why the authenticated context below has to send Basic preemptively.
  const anon = await browser.newContext({
    viewport: { width: 1500, height: 1000 }, deviceScaleFactor: 2 });
  const anonPage = await anon.newPage();
  await anonPage.goto(`${BASE}/app`, { waitUntil: 'networkidle' });
  const gated = await anonPage.locator("button:has-text('Sign in')").count();
  results['the auth gate refuses an unauthenticated browser'] = gated > 0;
  await shot(anonPage, '00-auth-gate.png');
  await anon.close();

  // Measured: Playwright's httpCredentials never reach this server, because
  // auth.py answers an unauthenticated HTML request with a redirect to /login
  // instead of a 401 challenge, and the context only attaches credentials on a
  // challenge. The explicit header is the same credential by the path app.py
  // documents for pre-existing callers, and it rides every XHR the page makes.
  const ctx = await browser.newContext({
    extraHTTPHeaders: {
      Authorization: 'Basic ' + Buffer.from('e2e:' + TOKEN).toString('base64'),
    },
    viewport: { width: 1500, height: 1000 },
    deviceScaleFactor: 2,
  });
  const page = await ctx.newPage();
  page.on('console', m => { if (m.type() === 'error') say(`    [console:error] ${m.text()}`); });
  page.on('pageerror', e => say(`    [pageerror] ${e}`));

  try {
    say('1. the dashboard loads over the real auth gate');
    await page.goto(`${BASE}/app`, { waitUntil: 'networkidle' });
    await page.waitForSelector('#company-list', { timeout: 30000 });
    await shot(page, '01-dashboard.png');

    say('2. FIX-2: the details stage narrates, and names columns not values');
    setMode('registered');
    await addCompany(page, '14123456');
    await selectCompany(page, '14123456');
    let r = await runStage(page, '14123456', 'details', 'Done');
    results['FIX2 details narrates into the drawer'] = r.drawer.includes('details: wrote');
    results['FIX2 details log does not print a director name'] =
      !r.drawer.includes('SMITH, Johnathan Archibald');
    await shot(page, '02-details-narrates.png');

    say('3. the domain stage, order ACCEPTED and registered');
    r = await runStage(page, '14123456', 'domain', 'Done');
    results['domain success shows the money log in the drawer'] =
      r.drawer.includes('domain REGISTERED') && r.drawer.includes('charged=8.88');
    await shot(page, '03-domain-registered.png');

    say('4. the domain stage, order REFUSED by Namecheap');
    setMode('refused');
    await addCompany(page, '14123457');
    await selectCompany(page, '14123457');
    r = await runStage(page, '14123457', 'domain', 'Failed');
    results['domain refused reads Failed, not Done'] = r.status.includes('Failed');
    // The money log says REFUSED, not "nothing charged": Namecheap does not
    // document that every Status=ERROR leaves nothing charged, so the line
    // records what is known and names the provider's error number.
    results['domain refused shows the reason'] =
      r.drawer.includes('REFUSED') && r.drawer.includes('claim is HELD')
      && r.drawer.includes('3050900');
    results['domain refused offers an Error Log button'] =
      (await page.locator("#srow-domain button:has-text('Error Log')").count()) > 0;
    await shot(page, '04-domain-refused.png');

    say('5. the domain stage, UNRESOLVED - money may have moved');
    setMode('unresolved');
    await addCompany(page, '14123458');
    await selectCompany(page, '14123458');
    r = await runStage(page, '14123458', 'domain', 'Failed');
    results['domain unresolved reads Failed'] = r.status.includes('Failed');
    results['domain unresolved warns that a charge may stand'] =
      r.drawer.includes('UNRESOLVED') && r.drawer.includes('claim KEPT');
    await shot(page, '05-domain-unresolved.png');

    say('6. FIX-1: the certificate stage with no document to download');
    setMode('nocert');
    await addCompany(page, '14123459');
    await selectCompany(page, '14123459');
    r = await runStage(page, '14123459', 'certificate', 'Failed');
    results['FIX1 certificate with nothing downloaded reads Failed'] = r.status.includes('Failed');
    results['FIX1 certificate failure says why'] =
      r.drawer.includes('nothing downloaded') || r.drawer.includes('No documents available');
    await shot(page, '06-certificate-failed.png');

    say('7. Run All Pending stops at the failing stage, before the purchase');
    await addCompany(page, '14123460');
    await selectCompany(page, '14123460');
    page.once('dialog', d => d.accept());
    await page.click('#run-all-btn');
    await page.waitForFunction(() => {
      const el = document.querySelector('#srow-certificate .stage-status-row');
      return el && el.innerText.includes('Failed');
    }, null, { timeout: 150000 });
    await sleep(4000);                       // let a wrongly-continuing chain show itself
    await page.click("button:has-text('Refresh')");
    await sleep(1500);
    const rows = {};
    for (const s of ['details', 'duns', 'certificate', 'domain', 'email', 'director_id', 'app']) {
      rows[s] = (await page.locator(`#srow-${s} .stage-status-row`).innerText()).trim();
      say(`    run-all: ${s.padEnd(12)} ${JSON.stringify(rows[s])}`);
    }
    results['run-all stops at the failed certificate'] = rows.certificate.includes('Failed');
    results['run-all never reached the billed domain stage'] = rows.domain.includes('Pending');
    results['run-all never reached the stages after it'] = rows.email.includes('Pending');
    await shot(page, '07-runall-stopped.png');

    say('9. a kept claim unsticks itself when the domain turns up in the account');
    setMode('unresolved');
    await addCompany(page, '14123461');
    await selectCompany(page, '14123461');
    r = await runStage(page, '14123461', 'domain', 'Failed');
    results['stuck claim: the first run fails and keeps the claim'] = r.status.includes('Failed');
    setMode('lands');                       // the registry catches up
    r = await runStage(page, '14123461', 'domain', 'Done');
    results['stuck claim: the re-run finishes it with NO human'] = r.status.includes('Done');
    results['stuck claim: resolved by reconciliation, not by a second order'] =
      r.drawer.includes('RESOLVED by reconciliation');
    await shot(page, '09-claim-self-healed.png');

    say('8. the drawer for a stage that never ran');
    await page.evaluate(() => openLog('app'));
    await sleep(1200);
    const unrun = await page.locator('#log-pre').innerText();
    say(`    drawer for an unrun stage: ${JSON.stringify(unrun)}`);
    results['an unrun stage still says "No output yet."'] = unrun.includes('No output yet.');
    await shot(page, '08-drawer-unrun.png');
  } catch (e) {
    say(`    [FATAL] ${e && e.message ? e.message : e}`);
    await shot(page, '99-failure-state.png');
    results['run completed without error'] = false;
  }

  await ctx.close();
  await browser.close();

  say('');
  say('END-TO-END RESULTS');
  let ok = true;
  for (const [k, v] of Object.entries(results)) {
    say(`  ${v ? 'PASS' : 'FAIL'}  ${k}`);
    ok = ok && !!v;
  }
  fs.writeFileSync(path.join(SHOTS, 'report.txt'), LOG.join('\n') + '\n');
  process.exit(ok ? 0 : 1);
})();
