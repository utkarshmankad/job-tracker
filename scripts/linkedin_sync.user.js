// ==UserScript==
// @name         Job Tracker — LinkedIn Sync
// @namespace    https://github.com/utkarshmankad/job-tracker
// @version      1.2.0
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

// DOM selectors — if LinkedIn breaks extraction, open DevTools on the Jobs
// Tracker page, find the job card elements, and update these.
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

function parseRelativeDate(text) {
  const m = text.match(/(\d+)\s+(day|week|month|year)s?\s+ago/i);
  if (!m) return null;
  const n = parseInt(m[1], 10);
  const d = new Date();
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
      method:  'POST',
      url:     API_URL,
      headers: { 'Content-Type': 'application/json' },
      data:    JSON.stringify(data),
      onload(res) {
        if (res.status >= 200 && res.status < 300) resolve(JSON.parse(res.responseText));
        else reject(new Error(`Server ${res.status}: ${res.responseText.slice(0, 200)}`));
      },
      onerror()   { reject(new Error('Cannot reach Job Tracker — is it running on port 8000?')); },
      ontimeout() { reject(new Error('Request timed out.')); },
    });
  });
}

// ─── DOM extraction (runs on the main document, not shadow DOM) ───────────────

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

function extractCompany(card, titleText) {
  if (!card) return null;
  for (const s of SEL.company) {
    const el = card.querySelector(s);
    if (!el) continue;
    const t = el.textContent.trim().split('·')[0].trim();
    if (t && t !== titleText && t.length >= 2 && t.length <= 120) return t;
  }
  const statusSet = new Set(KNOWN_STATUSES.map(s => s.toLowerCase()));
  for (const el of card.querySelectorAll('span, p, div')) {
    if (el.children.length > 0) continue;
    const t = el.textContent.trim();
    if (!t || t.length < 2 || t.length > 120) continue;
    if (t === titleText || DATE_NOISE.test(t) || statusSet.has(t.toLowerCase())) continue;
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
    const match = KNOWN_STATUSES.find(ks => ks.toLowerCase() === t.toLowerCase());
    if (match) return match;
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
  const seen = new Set();
  const jobs = [];
  for (const link of document.querySelectorAll(SEL.jobLink)) {
    const url = link.href.replace(/\?.*$/, '').replace(/\/*$/, '/');
    if (!url.match(/\/jobs\/view\/\d+/)) continue;
    if (seen.has(url)) continue;
    seen.add(url);
    const role = link.textContent.trim();
    if (!role || role.length < 3 || role.length > 200) continue;
    const card = findCard(link);
    jobs.push({
      role,
      company:         extractCompany(card, role),
      job_url:         url,
      applied_date:    extractDate(card),
      linkedin_status: extractStatus(card),
    });
  }
  return jobs;
}

async function loadAllJobs(onProgress) {
  let lastCount = -1, stable = 0, attempts = 0;
  while (stable < 3 && attempts < 40) {
    attempts++;
    const showMore = document.querySelector(SEL.showMore);
    if (showMore && !showMore.disabled) { showMore.click(); await sleep(1600); }
    const next = document.querySelector(SEL.nextPage);
    if (next && !next.disabled) { next.click(); await sleep(1800); }
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
    await sleep(1400);
    const count = document.querySelectorAll(SEL.jobLink).length;
    onProgress(count);
    stable = (count === lastCount) ? stable + 1 : 0;
    lastCount = count;
  }
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ─── Panel (Shadow DOM — completely isolated from LinkedIn's CSS) ─────────────

const PANEL_CSS = `
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :host { all: initial; }

  #trigger {
    display: flex; align-items: center; gap: 8px;
    background: #0A66C2; color: #fff;
    border: none; border-radius: 20px; padding: 10px 18px;
    cursor: pointer; font: 600 13px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    box-shadow: 0 2px 12px rgba(0,0,0,.3);
    white-space: nowrap;
  }
  #trigger:hover { background: #004182; }

  #box {
    display: none; flex-direction: column;
    width: 340px; max-height: 560px;
    background: #fff; border-radius: 12px;
    box-shadow: 0 6px 28px rgba(0,0,0,.22);
    font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    color: #1a1a1a; overflow: hidden;
  }
  #box.open { display: flex; }

  #header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 13px 15px; background: #0A66C2; color: #fff; flex-shrink: 0;
  }
  #header h3 { font-size: 14px; font-weight: 700; }
  #close {
    background: none; border: none; color: #fff; cursor: pointer;
    font-size: 18px; line-height: 1; opacity: .8; padding: 0;
    font-family: inherit;
  }
  #close:hover { opacity: 1; }

  #body { padding: 14px 15px 10px; overflow-y: auto; flex: 1; }

  #status {
    font-size: 12px; color: #555; min-height: 18px; margin-bottom: 10px;
  }

  #list {
    display: none; max-height: 220px; overflow-y: auto;
    border: 1px solid #ddd; border-radius: 8px; margin-bottom: 10px;
  }
  .job {
    display: flex; align-items: flex-start; gap: 8px;
    padding: 8px 10px; border-bottom: 1px solid #f0f0f0;
  }
  .job:last-child { border-bottom: none; }
  .job-info { flex: 1; min-width: 0; }
  .job-role {
    font-weight: 600; font-size: 12px; color: #1a1a1a;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .job-meta {
    font-size: 11px; color: #666; margin-top: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .badge {
    font-size: 10px; font-weight: 600; flex-shrink: 0;
    background: #e8f4fd; color: #0A66C2; border-radius: 10px;
    padding: 2px 7px; white-space: nowrap;
  }

  #result {
    display: none; border-radius: 8px; padding: 9px 11px;
    font-size: 12px; margin-bottom: 10px; white-space: pre-line;
    background: #edfaed; border: 1px solid #b7dfb7; color: #1e5c1e;
  }
  #result.error { background: #fff0f0; border-color: #f5c6cb; color: #721c24; }

  #footer {
    display: flex; gap: 8px; padding: 0 15px 14px; flex-shrink: 0;
  }
  button.btn {
    flex: 1; padding: 9px 0; border-radius: 8px; border: none;
    font: 600 13px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    cursor: pointer;
  }
  button.primary { background: #0A66C2; color: #fff; }
  button.primary:hover:not(:disabled) { background: #004182; }
  button.primary:disabled { opacity: .45; cursor: default; }
  button.secondary { background: #f0f0f0; color: #333; }
  button.secondary:hover { background: #e2e2e2; }

  .spin {
    display: inline-block; width: 11px; height: 11px; vertical-align: middle;
    border: 2px solid currentColor; border-top-color: transparent;
    border-radius: 50%; animation: spin .6s linear infinite; margin-right: 4px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
`;

const PANEL_HTML = `
  <button id="trigger">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853
        0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9
        1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337
        7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063
        2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0
        .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24
        23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/>
    </svg>
    Sync Jobs
  </button>
  <div id="box">
    <div id="header">
      <h3>LinkedIn → Job Tracker</h3>
      <button id="close" title="Collapse">✕</button>
    </div>
    <div id="body">
      <div id="status">Click Scan to load and extract all tracked jobs.</div>
      <div id="result"></div>
      <div id="list"></div>
    </div>
    <div id="footer">
      <button class="btn secondary" id="scan-btn">Scan</button>
      <button class="btn primary"   id="send-btn" disabled>Send</button>
    </div>
  </div>
`;

function createPanel() {
  // The host sits in the real DOM; its shadow root is invisible to LinkedIn's CSS.
  const host = document.createElement('div');
  host.id = 'jt-host';
  // All positioning is on the host via inline style (never touched by LinkedIn).
  host.style.cssText = [
    'position:fixed',
    'bottom:24px',
    'right:24px',
    // Max z-index — sits above LinkedIn's Messaging widget (~9000) and overlays.
    'z-index:2147483647',
    'display:block',
    'pointer-events:auto',
  ].join(';');
  document.body.appendChild(host);

  const shadow = host.attachShadow({ mode: 'open' });

  const styleEl = document.createElement('style');
  styleEl.textContent = PANEL_CSS;
  shadow.appendChild(styleEl);

  const wrapper = document.createElement('div');
  wrapper.innerHTML = PANEL_HTML;
  shadow.appendChild(wrapper);

  // All queries are against the shadow root — document.querySelector() would
  // not find elements inside a shadow DOM.
  const $ = sel => shadow.querySelector(sel);

  const trigger  = $('#trigger');
  const box      = $('#box');
  const closeBtn = $('#close');
  const statusEl = $('#status');
  const listEl   = $('#list');
  const resultEl = $('#result');
  const scanBtn  = $('#scan-btn');
  const sendBtn  = $('#send-btn');

  let jobs = [];

  function setStatus(html) { statusEl.innerHTML = html; }

  function renderList(data) {
    if (!data.length) { listEl.style.display = 'none'; return; }
    listEl.style.display = 'block';
    listEl.innerHTML = data.map(j => `
      <div class="job">
        <div class="job-info">
          <div class="job-role">${esc(j.role || '—')}</div>
          <div class="job-meta">
            ${esc(j.company || '(company unknown)')}${j.applied_date ? ' · ' + j.applied_date : ''}
          </div>
        </div>
        <span class="badge">${esc(j.linkedin_status)}</span>
      </div>
    `).join('');
  }

  function renderResult(res) {
    const parts = [
      res.created && `${res.created} created`,
      res.updated && `${res.updated} updated`,
      res.skipped && `${res.skipped} skipped`,
      res.errors  && `${res.errors} errors`,
    ].filter(Boolean);
    let text = parts.join(' · ') || 'Nothing changed.';
    if (res.warnings?.length) text += '\n⚠ ' + res.warnings.join('\n⚠ ');
    resultEl.textContent = text;
    resultEl.className   = res.errors ? 'error' : '';
    resultEl.style.display = 'block';
  }

  // ── Toggle ─────────────────────────────────────────────────────────────────
  trigger.addEventListener('click', () => {
    trigger.style.display = 'none';
    box.classList.add('open');
  });
  closeBtn.addEventListener('click', () => {
    box.classList.remove('open');
    trigger.style.display = '';
  });

  // ── Scan ───────────────────────────────────────────────────────────────────
  scanBtn.addEventListener('click', async () => {
    scanBtn.disabled = true;
    sendBtn.disabled = true;
    resultEl.style.display = 'none';
    listEl.style.display   = 'none';
    jobs = [];

    setStatus('<span class="spin"></span>Scrolling to load all jobs…');
    await loadAllJobs(n => setStatus(`<span class="spin"></span>Found ${n} job${n !== 1 ? 's' : ''}…`));

    jobs = extractJobs();
    scanBtn.disabled = false;

    if (!jobs.length) {
      setStatus("No jobs found. Make sure you're on the Jobs Tracker tab and the page has fully loaded.");
      return;
    }

    const unknownCo = jobs.filter(j => !j.company).length;
    setStatus(
      `Found <strong>${jobs.length}</strong> job${jobs.length !== 1 ? 's' : ''}` +
      (unknownCo ? ` (${unknownCo} with undetected company)` : '') +
      '. Review below, then click Send.'
    );
    renderList(jobs);
    sendBtn.disabled = false;
  });

  // ── Send ───────────────────────────────────────────────────────────────────
  sendBtn.addEventListener('click', async () => {
    if (!jobs.length) return;
    sendBtn.disabled = true;
    scanBtn.disabled = true;
    sendBtn.innerHTML = '<span class="spin"></span>Sending…';

    try {
      const res = await apiPost({ applications: jobs });
      renderResult(res);
      setStatus('Done! Your Job Tracker has been updated.');
    } catch (err) {
      resultEl.textContent = err.message;
      resultEl.className   = 'error';
      resultEl.style.display = 'block';
      setStatus('Send failed — see error above.');
    } finally {
      sendBtn.innerHTML = 'Send';
      sendBtn.disabled  = false;
      scanBtn.disabled  = false;
    }
  });
}

// ─── Tiny HTML escaper ────────────────────────────────────────────────────────
function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ─── Init (handles SPA navigation) ───────────────────────────────────────────

function isTrackerPage() {
  return /\/jobs[\/-]tracker/i.test(window.location.pathname);
}

function init() {
  if (!isTrackerPage()) return;
  if (document.getElementById('jt-host')) return;
  const check = setInterval(() => {
    if (document.querySelector('main, .scaffold-layout__main, #main')) {
      clearInterval(check);
      createPanel();
    }
  }, 500);
}

const _push = history.pushState.bind(history);
history.pushState = function (...args) { _push(...args); setTimeout(init, 800); };
window.addEventListener('popstate', () => setTimeout(init, 800));

init();
