// ==UserScript==
// @name         Job Tracker — LinkedIn Sync
// @namespace    https://github.com/utkarshmankad/job-tracker
// @version      1.1.0
// @description  Sync LinkedIn Jobs Tracker to your local Job Tracker app
// @match        https://www.linkedin.com/jobs-tracker*
// @match        https://www.linkedin.com/jobs/tracker*
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @connect      jobtracker.localhost
// @run-at       document-idle
// ==/UserScript==

/* global GM_xmlhttpRequest, GM_addStyle */
'use strict';

// ─── Config ───────────────────────────────────────────────────────────────────

const API_URL = 'http://jobtracker.localhost:8000/api/v1/applications/import/linkedin/json';

// DOM selectors — if LinkedIn breaks extraction, open DevTools on the Jobs
// Tracker page, find the job card elements, and update these.
const SEL = {
  // Most reliable: the job-posting link URL has never changed format.
  jobLink: 'a[href*="/jobs/view/"]',

  // Walk up from the link to find the card root. We try tag names first,
  // then class-name fragments. First match wins.
  cardTags:    ['LI', 'ARTICLE'],
  cardClasses: ['job-card', 'jobs-tracker', 'scaffold-layout__list-item',
                'artdeco-list__item'],

  // Company / subtitle — tried in order, first non-empty result wins.
  company: [
    '.artdeco-entity-lockup__subtitle',
    '[class*="primary-description"]',
    '[class*="subtitle"]',
    '[class*="company-name"]',
    'h4',
  ],

  // Status badge — tried in order.
  status: [
    '[class*="tracking-status"]',
    '[class*="job-tracking"]',
    '[class*="status-label"]',
    '[class*="job-card-list__footer"] span',
  ],

  // Pagination / infinite-scroll controls.
  showMore: [
    'button[aria-label*="more results" i]',
    'button[aria-label*="show more" i]',
    '.infinite-scroller__show-more-button',
  ].join(','),
  nextPage: [
    '.artdeco-pagination__button--next',
    'button[aria-label="Next" i]',
  ].join(','),
};

// All LinkedIn status strings the script can recognise (checked in order
// of length, longest first, to avoid partial matches).
const KNOWN_STATUSES = [
  'Interview scheduled', 'Interview in progress',
  'Application viewed', 'Not selected',
  'Offer extended', 'Offer received',
  'Interviewing', 'In review',
  'Withdrawn', 'Rejected',
  'Applied', 'Offered', 'Saved',
];

// Noise patterns to exclude when guessing company name from free text.
const DATE_NOISE = /\b(\d+\s*(day|week|month|year)|ago|applied|saved|just now)/i;

// ─── Utilities ────────────────────────────────────────────────────────────────

const sleep = ms => new Promise(r => setTimeout(r, ms));

/** Parse "3 weeks ago" → YYYY-MM-DD. Returns null if the text doesn't match. */
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

/** POST JSON to the local API via GM_xmlhttpRequest (bypasses CORS). */
function apiPost(data) {
  return new Promise((resolve, reject) => {
    GM_xmlhttpRequest({
      method:  'POST',
      url:     API_URL,
      headers: { 'Content-Type': 'application/json' },
      data:    JSON.stringify(data),
      onload(res) {
        if (res.status >= 200 && res.status < 300) {
          resolve(JSON.parse(res.responseText));
        } else {
          reject(new Error(`Server returned ${res.status}: ${res.responseText.slice(0, 120)}`));
        }
      },
      onerror() { reject(new Error('Could not reach Job Tracker — is it running on port 8000?')); },
      ontimeout() { reject(new Error('Request timed out.')); },
    });
  });
}

// ─── DOM extraction ───────────────────────────────────────────────────────────

/** Walk up from a link to find the job-card root element. */
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

/** Extract company name from a card element, excluding the job title. */
function extractCompany(card, titleText) {
  if (!card) return null;

  // Strategy 1: known subtitle selectors.
  for (const s of SEL.company) {
    const el = card.querySelector(s);
    if (!el) continue;
    const t = el.textContent.trim().split('·')[0].trim();
    if (t && t !== titleText && t.length >= 2 && t.length <= 120) return t;
  }

  // Strategy 2: scan leaf text nodes, exclude title, dates, and status labels.
  const statusSet = new Set(KNOWN_STATUSES.map(s => s.toLowerCase()));
  for (const el of card.querySelectorAll('span, p, div')) {
    if (el.children.length > 0) continue;        // skip containers
    const t = el.textContent.trim();
    if (!t || t.length < 2 || t.length > 120) continue;
    if (t === titleText) continue;
    if (DATE_NOISE.test(t)) continue;
    if (statusSet.has(t.toLowerCase())) continue;
    return t.split('·')[0].trim();
  }
  return null;
}

/** Extract the application status from a card element. */
function extractStatus(card) {
  if (!card) return 'Applied';

  // Strategy 1: specific status selectors.
  for (const s of SEL.status) {
    const el = card.querySelector(s);
    if (!el) continue;
    const t = el.textContent.trim();
    const match = KNOWN_STATUSES.find(ks => ks.toLowerCase() === t.toLowerCase());
    if (match) return match;
  }

  // Strategy 2: scan all card text for known status strings (longest first
  // so "Interviewing" is matched before the substring "In").
  const text = card.textContent;
  for (const s of KNOWN_STATUSES) {
    if (text.includes(s)) return s;
  }
  return 'Applied';
}

/** Extract the applied date from a card element. */
function extractDate(card) {
  if (!card) return null;
  const t = card.querySelector('time[datetime]');
  if (t) return t.getAttribute('datetime').slice(0, 10);
  return parseRelativeDate(card.textContent);
}

/** Collect all visible job cards on the current page view. */
function extractJobs() {
  const seen = new Set();
  const jobs = [];

  for (const link of document.querySelectorAll(SEL.jobLink)) {
    // Normalise URL: strip query params, ensure trailing slash.
    const url = link.href.replace(/\?.*$/, '').replace(/\/*$/, '/');
    if (!url.match(/\/jobs\/view\/\d+/)) continue;
    if (seen.has(url)) continue;
    seen.add(url);

    const role = link.textContent.trim();
    // Skip non-title links like "Apply" buttons (too short or generic).
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

// ─── Scroll / load all pages ─────────────────────────────────────────────────

/**
 * Auto-scroll / paginate until the job count is stable for 3 consecutive
 * rounds. Calls onProgress(count) after each scroll attempt.
 *
 * Strategy (tried each round in order):
 *   1. Click a "Show more" / "Load more" button if present.
 *   2. Click the pagination "Next" button if present.
 *   3. Scroll to the bottom to trigger infinite-scroll.
 */
async function loadAllJobs(onProgress) {
  let lastCount = -1;
  let stable = 0;
  let attempts = 0;

  while (stable < 3 && attempts < 40) {
    attempts++;

    const showMore = document.querySelector(SEL.showMore);
    if (showMore && !showMore.disabled) {
      showMore.click();
      await sleep(1600);
    }

    const next = document.querySelector(SEL.nextPage);
    if (next && !next.disabled && !next.hasAttribute('disabled')) {
      next.click();
      await sleep(1800);
    }

    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
    await sleep(1400);

    const count = document.querySelectorAll(SEL.jobLink).length;
    onProgress(count);

    stable = (count === lastCount) ? stable + 1 : 0;
    lastCount = count;
  }

  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ─── Panel ────────────────────────────────────────────────────────────────────

GM_addStyle(`
  #jt-panel {
    position: fixed; bottom: 24px; right: 24px; z-index: 99999;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 13px; line-height: 1.45;
  }

  /* Collapsed trigger button */
  #jt-trigger {
    display: flex; align-items: center; gap: 7px;
    background: #0A66C2; color: #fff;
    border: none; border-radius: 20px; padding: 9px 16px;
    cursor: pointer; font-size: 13px; font-weight: 600;
    box-shadow: 0 2px 12px rgba(0,0,0,.25);
    transition: background .15s;
  }
  #jt-trigger:hover { background: #004182; }
  #jt-trigger svg  { flex-shrink: 0; }

  /* Expanded panel */
  #jt-box {
    display: none;
    flex-direction: column;
    width: 340px;
    background: #fff;
    border-radius: 12px;
    box-shadow: 0 4px 24px rgba(0,0,0,.18);
    overflow: hidden;
  }
  #jt-box.open { display: flex; }

  #jt-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 16px 12px;
    background: #0A66C2; color: #fff;
  }
  #jt-header h3 { margin: 0; font-size: 14px; font-weight: 700; }
  #jt-close-btn {
    background: none; border: none; color: #fff; cursor: pointer;
    font-size: 18px; line-height: 1; padding: 0 2px;
    opacity: .8;
  }
  #jt-close-btn:hover { opacity: 1; }

  #jt-body { padding: 14px 16px; }

  #jt-status {
    color: #555; font-size: 12px; margin-bottom: 10px; min-height: 18px;
  }

  /* Job preview list */
  #jt-list {
    max-height: 220px; overflow-y: auto;
    border: 1px solid #e0e0e0; border-radius: 8px;
    margin-bottom: 12px; display: none;
  }
  .jt-job {
    padding: 8px 10px;
    border-bottom: 1px solid #f0f0f0;
    display: flex; gap: 8px; align-items: flex-start;
  }
  .jt-job:last-child { border-bottom: none; }
  .jt-job-info { flex: 1; min-width: 0; }
  .jt-job-role { font-weight: 600; color: #1a1a1a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .jt-job-meta { color: #666; font-size: 11px; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .jt-badge {
    font-size: 10px; font-weight: 600; border-radius: 10px;
    padding: 2px 7px; white-space: nowrap; flex-shrink: 0;
    background: #e8f4fd; color: #0A66C2;
  }

  /* Result summary */
  #jt-result {
    display: none; background: #f0f8f0; border: 1px solid #c3e6cb;
    border-radius: 8px; padding: 10px 12px; font-size: 12px;
    color: #2d6a2d; margin-bottom: 12px;
  }
  #jt-result.error { background: #fff0f0; border-color: #f5c6cb; color: #721c24; }

  /* Spinner */
  .jt-spin {
    display: inline-block; width: 12px; height: 12px;
    border: 2px solid #fff; border-top-color: transparent;
    border-radius: 50%; animation: jt-spin .6s linear infinite;
    vertical-align: middle; margin-right: 5px;
  }
  @keyframes jt-spin { to { transform: rotate(360deg); } }

  /* Buttons */
  #jt-footer { padding: 0 16px 14px; display: flex; gap: 8px; }
  .jt-btn {
    flex: 1; padding: 8px 0; border-radius: 8px;
    font-size: 13px; font-weight: 600; cursor: pointer;
    border: none; transition: background .15s;
  }
  .jt-btn-primary { background: #0A66C2; color: #fff; }
  .jt-btn-primary:hover:not(:disabled) { background: #004182; }
  .jt-btn-primary:disabled { opacity: .5; cursor: default; }
  .jt-btn-secondary { background: #f3f3f3; color: #444; }
  .jt-btn-secondary:hover { background: #e6e6e6; }

  @media (prefers-color-scheme: dark) {
    #jt-box { background: #1b1f23; color: #e6e6e6; }
    #jt-list { border-color: #333; }
    .jt-job { border-bottom-color: #2a2a2a; }
    .jt-job-role { color: #e6e6e6; }
    .jt-job-meta { color: #999; }
    .jt-btn-secondary { background: #2a2a2a; color: #ccc; }
  }
`);

function createPanel() {
  const root = document.createElement('div');
  root.id = 'jt-panel';
  root.innerHTML = `
    <button id="jt-trigger" title="Sync to Job Tracker">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
        <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037
          -1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046
          c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286z
          M5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063
          2.065zm1.782 13.019H3.555V9h3.564v11.452z
          M22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771
          24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/>
      </svg>
      Sync Jobs
    </button>

    <div id="jt-box">
      <div id="jt-header">
        <h3>LinkedIn → Job Tracker</h3>
        <button id="jt-close-btn" title="Collapse">✕</button>
      </div>
      <div id="jt-body">
        <div id="jt-status">Click Scan to load and extract all tracked jobs.</div>
        <div id="jt-result"></div>
        <div id="jt-list"></div>
      </div>
      <div id="jt-footer">
        <button class="jt-btn jt-btn-secondary" id="jt-scan-btn">Scan</button>
        <button class="jt-btn jt-btn-primary" id="jt-send-btn" disabled>Send</button>
      </div>
    </div>
  `;
  document.body.appendChild(root);

  const trigger  = root.querySelector('#jt-trigger');
  const box      = root.querySelector('#jt-box');
  const closeBtn = root.querySelector('#jt-close-btn');
  const status   = root.querySelector('#jt-status');
  const list     = root.querySelector('#jt-list');
  const result   = root.querySelector('#jt-result');
  const scanBtn  = root.querySelector('#jt-scan-btn');
  const sendBtn  = root.querySelector('#jt-send-btn');

  let jobs = [];

  function setStatus(msg) { status.textContent = msg; }

  function showList(data) {
    list.innerHTML = '';
    if (!data.length) { list.style.display = 'none'; return; }
    list.style.display = 'block';
    list.innerHTML = data.map(j => `
      <div class="jt-job">
        <div class="jt-job-info">
          <div class="jt-job-role" title="${(j.role || '').replace(/"/g, '&quot;')}">${j.role || '—'}</div>
          <div class="jt-job-meta">
            ${j.company || '<em>company unknown</em>'}
            ${j.applied_date ? ' · ' + j.applied_date : ''}
          </div>
        </div>
        <span class="jt-badge">${j.linkedin_status}</span>
      </div>
    `).join('');
  }

  function showResult(res) {
    const parts = [
      res.created  && `${res.created} created`,
      res.updated  && `${res.updated} updated`,
      res.skipped  && `${res.skipped} skipped`,
      res.errors   && `${res.errors} errors`,
    ].filter(Boolean);
    result.textContent = parts.join(' · ') || 'Nothing changed.';
    result.className = res.errors ? 'error' : '';
    result.style.display = 'block';
    if (res.warnings?.length) {
      result.textContent += '\n⚠ ' + res.warnings.join('\n⚠ ');
    }
  }

  // Toggle between collapsed button and expanded panel.
  trigger.addEventListener('click', () => {
    trigger.style.display = 'none';
    box.classList.add('open');
  });
  closeBtn.addEventListener('click', () => {
    box.classList.remove('open');
    trigger.style.display = '';
  });

  // ── Scan ────────────────────────────────────────────────────────────────────
  scanBtn.addEventListener('click', async () => {
    scanBtn.disabled = true;
    sendBtn.disabled = true;
    result.style.display = 'none';
    list.style.display   = 'none';
    setStatus('<span class="jt-spin"></span>Scrolling to load all jobs…');
    status.innerHTML = status.textContent; // allow HTML for spinner

    await loadAllJobs(count => {
      status.innerHTML = `<span class="jt-spin"></span>Found ${count} job link${count !== 1 ? 's' : ''}…`;
    });

    jobs = extractJobs();
    scanBtn.disabled = false;

    if (!jobs.length) {
      setStatus('No jobs found. Make sure you\'re on the Jobs Tracker tab and the page has loaded.');
      return;
    }

    const unknown = jobs.filter(j => !j.company).length;
    setStatus(
      `Found ${jobs.length} job${jobs.length !== 1 ? 's' : ''}` +
      (unknown ? ` (${unknown} with unknown company — will still be imported)` : '') +
      '. Review below, then click Send.'
    );
    showList(jobs);
    sendBtn.disabled = false;
  });

  // ── Send ────────────────────────────────────────────────────────────────────
  sendBtn.addEventListener('click', async () => {
    if (!jobs.length) return;
    sendBtn.disabled = true;
    scanBtn.disabled = true;
    sendBtn.innerHTML = '<span class="jt-spin"></span>Sending…';

    try {
      const res = await apiPost({ applications: jobs });
      showResult(res);
      setStatus('Done! Your Job Tracker has been updated.');
    } catch (err) {
      result.textContent = err.message;
      result.className   = 'error';
      result.style.display = 'block';
      setStatus('Send failed — see error above.');
    } finally {
      sendBtn.innerHTML = 'Send';
      sendBtn.disabled  = false;
      scanBtn.disabled  = false;
    }
  });
}

// ─── Init ─────────────────────────────────────────────────────────────────────

// LinkedIn is a SPA — the script fires once on hard-load. If the user
// navigates away and back within LinkedIn, we detect the URL change and
// re-inject the panel if needed.

function isTrackerPage() {
  return /\/jobs[\/-]tracker/i.test(window.location.pathname);
}

function init() {
  if (!isTrackerPage()) return;
  if (document.getElementById('jt-panel')) return; // already injected
  // Wait for the main content container to appear before injecting.
  const check = setInterval(() => {
    if (document.querySelector('main, .scaffold-layout__main, #main')) {
      clearInterval(check);
      createPanel();
    }
  }, 500);
}

// Intercept SPA navigation.
const _push = history.pushState.bind(history);
history.pushState = function (...args) {
  _push(...args);
  setTimeout(init, 800);
};
window.addEventListener('popstate', () => setTimeout(init, 800));

init();
