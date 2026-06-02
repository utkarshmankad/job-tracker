// ==UserScript==
// @name         Job Tracker — LinkedIn Sync
// @namespace    https://github.com/utkarshmankad/job-tracker
// @version      1.4.0
// @description  Sync LinkedIn Jobs Tracker to your local Job Tracker app
// @match        https://www.linkedin.com/jobs-tracker*
// @match        https://www.linkedin.com/jobs/tracker*
// @grant        GM_xmlhttpRequest
// @connect      jobtracker.localhost
// @run-at       document-idle
// ==/UserScript==

/* global GM_xmlhttpRequest */
'use strict';

// ─── Config ───────────────────────────────────────────────────────────────────

const API_URL = 'http://jobtracker.localhost:8000/api/v1/applications/import/linkedin/json';

const SEL = {
  jobLink:     'a[href*="/jobs/view/"]',
  cardTags:    ['LI', 'ARTICLE'],
  cardClasses: ['job-card', 'jobs-tracker', 'scaffold-layout__list-item', 'artdeco-list__item'],
  company: [
    '.artdeco-entity-lockup__subtitle',
    '[class*="primary-description"]',
    '[class*="subtitle"]',
    '[class*="company-name"]',
    'h4',
  ],
  status: [
    '[class*="tracking-status"]',
    '[class*="job-tracking"]',
    '[class*="status-label"]',
    '[class*="job-card-list__footer"] span',
  ],
  showMore: 'button[aria-label*="more results" i], button[aria-label*="show more" i], .infinite-scroller__show-more-button',
  nextPage:  '.artdeco-pagination__button--next, button[aria-label="Next" i]',
};

const KNOWN_STATUSES = [
  'Interview scheduled', 'Interview in progress',
  'Application viewed', 'Not selected',
  'Offer extended', 'Offer received',
  'Interviewing', 'In review',
  'Withdrawn', 'Rejected',
  'Applied', 'Offered', 'Saved',
];
const DATE_NOISE = /\b(\d+\s*(day|week|month|year)|ago|applied|saved|just now)/i;

// ─── Utilities ────────────────────────────────────────────────────────────────

const sleep = ms => new Promise(r => setTimeout(r, ms));

// Element factory — uses createElement only, zero innerHTML, bypasses
// LinkedIn's Trusted Types / sanitizeHTML policy.
function mk(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls)  e.className   = cls;
  if (text != null) e.textContent = text;
  return e;
}

function mkSVG(tag, attrs) {
  const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

// Build a spinner <span> inside a button without touching innerHTML.
function setSpinText(btn, label) {
  btn.textContent = '';
  btn.appendChild(mk('span', 'spin'));
  btn.appendChild(document.createTextNode(' ' + label));
}

function parseRelativeDate(text) {
  const m = text.match(/(\d+)\s+(day|week|month|year)s?\s+ago/i);
  if (!m) return null;
  const n = parseInt(m[1], 10), d = new Date();
  switch (m[2].toLowerCase()) {
    case 'day':   d.setDate(d.getDate() - n); break;
    case 'week':  d.setDate(d.getDate() - n * 7); break;
    case 'month': d.setMonth(d.getMonth() - n); break;
    case 'year':  d.setFullYear(d.getFullYear() - n); break;
  }
  return d.toISOString().slice(0, 10);
}

function apiPost(data) {
  return new Promise((resolve, reject) => {
    GM_xmlhttpRequest({
      method: 'POST', url: API_URL,
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify(data),
      onload(r) {
        r.status >= 200 && r.status < 300
          ? resolve(JSON.parse(r.responseText))
          : reject(new Error(`Server ${r.status}: ${r.responseText.slice(0, 200)}`));
      },
      onerror()   { reject(new Error('Cannot reach Job Tracker — is it running on port 8000?')); },
      ontimeout() { reject(new Error('Request timed out.')); },
    });
  });
}

// ─── DOM extraction ───────────────────────────────────────────────────────────

function findCard(link) {
  let el = link.parentElement;
  for (let i = 0; i < 12; i++) {
    if (!el) break;
    if (SEL.cardTags.includes(el.tagName)) return el;
    const cls = (el.className || '').toLowerCase();
    if (SEL.cardClasses.some(c => cls.includes(c))) return el;
    el = el.parentElement;
  }
  return link.closest('li') || link.parentElement;
}

function extractCompany(card, title) {
  if (!card) return null;
  for (const s of SEL.company) {
    const el = card.querySelector(s);
    if (!el) continue;
    const t = el.textContent.trim().split('·')[0].trim();
    if (t && t !== title && t.length >= 2 && t.length <= 120) return t;
  }
  const skip = new Set(KNOWN_STATUSES.map(s => s.toLowerCase()));
  for (const el of card.querySelectorAll('span, p, div')) {
    if (el.children.length > 0) continue;
    const t = el.textContent.trim();
    if (!t || t.length < 2 || t.length > 120) continue;
    if (t === title || DATE_NOISE.test(t) || skip.has(t.toLowerCase())) continue;
    return t.split('·')[0].trim();
  }
  return null;
}

function extractStatus(card) {
  if (!card) return 'Applied';
  for (const s of SEL.status) {
    const el = card.querySelector(s);
    if (!el) continue;
    const t = el.textContent.trim();
    const m = KNOWN_STATUSES.find(k => k.toLowerCase() === t.toLowerCase());
    if (m) return m;
  }
  const text = card.textContent;
  for (const s of KNOWN_STATUSES) { if (text.includes(s)) return s; }
  return 'Applied';
}

function extractDate(card) {
  if (!card) return null;
  const t = card.querySelector('time[datetime]');
  if (t) return t.getAttribute('datetime').slice(0, 10);
  return parseRelativeDate(card.textContent);
}

function extractJobs() {
  const seen = new Set(), jobs = [];
  for (const link of document.querySelectorAll(SEL.jobLink)) {
    const url = link.href.replace(/\?.*$/, '').replace(/\/*$/, '/');
    if (!url.match(/\/jobs\/view\/\d+/) || seen.has(url)) continue;
    seen.add(url);
    const role = link.textContent.trim();
    if (!role || role.length < 3 || role.length > 200) continue;
    const card = findCard(link);
    jobs.push({ role, company: extractCompany(card, role), job_url: url,
                applied_date: extractDate(card), linkedin_status: extractStatus(card) });
  }
  return jobs;
}

async function loadAllJobs(onProgress) {
  let last = -1, stable = 0, tries = 0;
  while (stable < 3 && tries < 40) {
    tries++;
    const more = document.querySelector(SEL.showMore);
    if (more && !more.disabled) { more.click(); await sleep(1600); }
    const next = document.querySelector(SEL.nextPage);
    if (next && !next.disabled) { next.click(); await sleep(1800); }
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
    await sleep(1400);
    const n = document.querySelectorAll(SEL.jobLink).length;
    onProgress(n);
    stable = (n === last) ? stable + 1 : 0;
    last = n;
  }
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ─── Panel CSS ────────────────────────────────────────────────────────────────

const PANEL_CSS = `
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:host { all: initial; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }

.jt-trigger {
  display: flex; align-items: center; gap: 8px;
  background: #0A66C2; color: #fff;
  border: none; border-radius: 20px; padding: 10px 18px;
  cursor: pointer; font: 600 13px/1 inherit;
  box-shadow: 0 2px 12px rgba(0,0,0,.3); white-space: nowrap;
}
.jt-trigger:hover { background: #004182; }

.jt-box {
  display: none; flex-direction: column;
  width: 340px; max-height: 560px;
  background: #fff; border-radius: 12px;
  box-shadow: 0 6px 28px rgba(0,0,0,.22);
  font: 13px/1.45 inherit; color: #1a1a1a; overflow: hidden;
}
.jt-box.open { display: flex; }

.jt-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 13px 15px; background: #0A66C2; color: #fff; flex-shrink: 0;
}
.jt-header h3 { font-size: 14px; font-weight: 700; }
.jt-close {
  background: none; border: none; color: #fff; cursor: pointer;
  font-size: 18px; line-height: 1; opacity: .8; padding: 0; font-family: inherit;
}
.jt-close:hover { opacity: 1; }

.jt-body { padding: 14px 15px 10px; overflow-y: auto; flex: 1; }

.jt-status { font-size: 12px; color: #555; min-height: 18px; margin-bottom: 10px; }

.jt-list {
  display: none; max-height: 220px; overflow-y: auto;
  border: 1px solid #ddd; border-radius: 8px; margin-bottom: 10px;
}
.jt-job {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 8px 10px; border-bottom: 1px solid #f0f0f0;
}
.jt-job:last-child { border-bottom: none; }
.jt-job-info { flex: 1; min-width: 0; }
.jt-job-role { font-weight: 600; font-size: 12px; color: #1a1a1a;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.jt-job-meta { font-size: 11px; color: #666; margin-top: 2px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.jt-badge { font-size: 10px; font-weight: 600; flex-shrink: 0;
  background: #e8f4fd; color: #0A66C2; border-radius: 10px; padding: 2px 7px; }

.jt-result {
  display: none; border-radius: 8px; padding: 9px 11px;
  font-size: 12px; margin-bottom: 10px; white-space: pre-line;
  background: #edfaed; border: 1px solid #b7dfb7; color: #1e5c1e;
}
.jt-result.error { background: #fff0f0; border-color: #f5c6cb; color: #721c24; }

.jt-footer { display: flex; gap: 8px; padding: 0 15px 14px; flex-shrink: 0; }
.jt-btn {
  flex: 1; padding: 9px 0; border-radius: 8px; border: none;
  font: 600 13px/1 inherit; cursor: pointer;
}
.jt-primary { background: #0A66C2; color: #fff; }
.jt-primary:hover:not(:disabled) { background: #004182; }
.jt-primary:disabled { opacity: .45; cursor: default; }
.jt-secondary { background: #f0f0f0; color: #333; }
.jt-secondary:hover { background: #e2e2e2; }

.spin {
  display: inline-block; width: 11px; height: 11px; vertical-align: middle;
  border: 2px solid currentColor; border-top-color: transparent;
  border-radius: 50%; animation: jtSpin .6s linear infinite;
}
@keyframes jtSpin { to { transform: rotate(360deg); } }
`;

// ─── Panel construction (zero innerHTML — bypasses LinkedIn's sanitizeHTML) ───

function createPanel() {
  console.log('[JobTracker] createPanel() called');

  // ── Host + Shadow DOM ──────────────────────────────────────────────────────
  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:2147483647;display:block;pointer-events:auto;';
  document.body.appendChild(host);

  const shadow = host.attachShadow({ mode: 'open' });
  const style  = document.createElement('style');
  style.textContent = PANEL_CSS;   // <style> textContent is never sanitized
  shadow.appendChild(style);
  console.log('[JobTracker] Shadow DOM + styles attached');

  // ── LinkedIn logo SVG (createElementNS, no innerHTML) ─────────────────────
  const svg  = mkSVG('svg', { width: '15', height: '15', viewBox: '0 0 24 24', fill: 'currentColor', 'aria-hidden': 'true' });
  const path = mkSVG('path', { d: 'M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z' });
  svg.appendChild(path);

  // ── Build elements with createElement — no innerHTML touches ──────────────

  // Trigger (collapsed state)
  const trigger = mk('button', 'jt-trigger');
  trigger.title = 'Sync to Job Tracker';
  trigger.appendChild(svg);
  trigger.appendChild(document.createTextNode(' Sync Jobs'));

  // Panel box elements — keep direct references, no querySelector needed
  const h3        = mk('h3',     null,             'LinkedIn → Job Tracker');
  const closeBtn  = mk('button', 'jt-close',       '✕');
  closeBtn.title  = 'Collapse';

  const statusEl  = mk('div',    'jt-status',      'Click Scan to load and extract all tracked jobs.');
  const resultEl  = mk('div',    'jt-result');
  const listEl    = mk('div',    'jt-list');

  const scanBtn   = mk('button', 'jt-btn jt-secondary', 'Scan');
  const sendBtn   = mk('button', 'jt-btn jt-primary',   'Send');
  sendBtn.disabled = true;

  // Assemble tree
  const header = mk('div', 'jt-header');
  header.appendChild(h3);
  header.appendChild(closeBtn);

  const body = mk('div', 'jt-body');
  body.appendChild(statusEl);
  body.appendChild(resultEl);
  body.appendChild(listEl);

  const footer = mk('div', 'jt-footer');
  footer.appendChild(scanBtn);
  footer.appendChild(sendBtn);

  const box = mk('div', 'jt-box');
  box.appendChild(header);
  box.appendChild(body);
  box.appendChild(footer);

  shadow.appendChild(trigger);
  shadow.appendChild(box);
  console.log('[JobTracker] Panel tree built — scanBtn:', scanBtn, '| sendBtn:', sendBtn);

  // ── Helpers ────────────────────────────────────────────────────────────────

  function setStatus(text, spin = false) {
    statusEl.textContent = '';
    if (spin) statusEl.appendChild(mk('span', 'spin'));
    statusEl.appendChild(document.createTextNode(spin ? ' ' + text : text));
  }

  function setResult(text, isError = false) {
    resultEl.textContent = text;
    resultEl.className   = 'jt-result' + (isError ? ' error' : '');
    resultEl.style.display = 'block';
  }

  function renderList(jobs) {
    listEl.textContent = '';
    if (!jobs.length) { listEl.style.display = 'none'; return; }
    listEl.style.display = 'block';
    for (const j of jobs) {
      const row  = mk('div',  'jt-job');
      const info = mk('div',  'jt-job-info');
      info.appendChild(mk('div', 'jt-job-role', j.role || '—'));
      info.appendChild(mk('div', 'jt-job-meta',
        (j.company || 'company unknown') + (j.applied_date ? ' · ' + j.applied_date : '')));
      row.appendChild(info);
      row.appendChild(mk('span', 'jt-badge', j.linkedin_status));
      listEl.appendChild(row);
    }
  }

  // ── Event handlers ─────────────────────────────────────────────────────────

  let jobs = [];

  trigger.addEventListener('click', () => {
    trigger.style.display = 'none';
    box.classList.add('open');
  });

  closeBtn.addEventListener('click', () => {
    box.classList.remove('open');
    trigger.style.display = '';
  });

  scanBtn.addEventListener('click', async () => {
    scanBtn.disabled = true;
    sendBtn.disabled = true;
    resultEl.style.display = 'none';
    listEl.style.display   = 'none';
    jobs = [];

    setStatus('Scrolling to load all jobs…', true);
    await loadAllJobs(n => setStatus(`Found ${n} job${n !== 1 ? 's' : ''}…`, true));

    jobs = extractJobs();
    scanBtn.disabled = false;

    if (!jobs.length) {
      setStatus("No jobs found. Make sure you're on the Jobs Tracker tab and the page has loaded.");
      return;
    }

    const unk = jobs.filter(j => !j.company).length;
    setStatus(`Found ${jobs.length} job${jobs.length !== 1 ? 's' : ''}${unk ? ` (${unk} with unknown company)` : ''}. Review below, then click Send.`);
    renderList(jobs);
    sendBtn.disabled = false;
  });

  sendBtn.addEventListener('click', async () => {
    if (!jobs.length) return;
    sendBtn.disabled = true;
    scanBtn.disabled = true;
    setSpinText(sendBtn, 'Sending…');

    try {
      const res = await apiPost({ applications: jobs });
      const parts = [
        res.created && `${res.created} created`,
        res.updated && `${res.updated} updated`,
        res.skipped && `${res.skipped} skipped`,
        res.errors  && `${res.errors} errors`,
      ].filter(Boolean);
      let text = parts.join(' · ') || 'Nothing changed.';
      if (res.warnings?.length) text += '\n⚠ ' + res.warnings.join('\n⚠ ');
      setResult(text, !!res.errors);
      setStatus('Done! Your Job Tracker has been updated.');
    } catch (err) {
      setResult(err.message, true);
      setStatus('Send failed — see error above.');
    } finally {
      sendBtn.textContent = 'Send';
      sendBtn.disabled  = false;
      scanBtn.disabled  = false;
    }
  });

  console.log('[JobTracker] Event listeners attached.');
}

// ─── Init ─────────────────────────────────────────────────────────────────────

function isTrackerPage() {
  return /\/jobs[\/-]tracker/i.test(window.location.pathname);
}

function init() {
  if (!isTrackerPage()) return;
  if (document.querySelector('[data-jt-host]')) return;

  // Mark intent immediately so concurrent calls don't double-init.
  const marker = document.createElement('meta');
  marker.setAttribute('data-jt-host', '1');
  document.head.appendChild(marker);

  console.log('[JobTracker] Scheduling panel mount…');
  setTimeout(() => {
    try {
      createPanel();
      console.log('[JobTracker] Panel mounted.');
    } catch (err) {
      console.error('[JobTracker] createPanel threw:', err);
      marker.remove();  // allow retry
    }
  }, 2000);
}

const _push = history.pushState.bind(history);
history.pushState = function (...a) { _push(...a); setTimeout(init, 1000); };
window.addEventListener('popstate', () => setTimeout(init, 1000));

init();
