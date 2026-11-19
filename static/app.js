let PID = Number(localStorage.getItem('pid') || 0);
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// on-page messages (browser popups are blocked in some embedded browsers)
function toast(msg, kind = 'ok') {
  let box = $('#toasts');
  if (!box) { box = document.createElement('div'); box.id = 'toasts'; document.body.appendChild(box); }
  const t = document.createElement('div'); t.className = 'toast ' + kind; t.textContent = msg;
  box.appendChild(t); setTimeout(() => t.remove(), kind === 'bad' ? 7000 : 3500);
}
const errText = t => { try { return JSON.parse(t).detail || t; } catch { return t; } };
async function api(path, opts = {}) {
  const r = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...opts,
    body: opts.body && typeof opts.body !== 'string' && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body});
  if (!r.ok) { const t = await r.text(); toast(errText(t), 'bad'); throw new Error(t); }
  return r.json();
}

// ---------- tabs ----------
document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));
function showTab(t) {
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  document.querySelectorAll('.tab').forEach(s => s.classList.toggle('active', s.id === t));
  ({jobs: loadJobs, pending: loadPending, profile: loadProfile, answers: loadAnswers, avoid: loadAvoid,
    emails: loadEmails, settings: loadSettings, dash: refresh})[t]?.();
}

// ---------- profiles ----------
async function loadProfiles() {
  const ps = await api('/api/profiles');
  if (!ps.find(p => p.id === PID)) PID = ps[0].id;
  $('#profileSel').innerHTML = ps.map(p => `<option value="${p.id}" ${p.id === PID ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
}
$('#profileSel').onchange = e => { PID = Number(e.target.value); localStorage.setItem('pid', PID); showTab('dash'); };
$('#newProfile').onclick = () => { $('#newProfileBox').classList.toggle('hidden'); $('#newProfileName').focus(); };
$('#newProfileName').onkeydown = e => { if (e.key === 'Enter') createProfile(); };
async function createProfile() {
  const name = $('#newProfileName').value.trim(); if (!name) return toast('Type a name first', 'bad');
  $('#newProfileName').value = ''; $('#newProfileBox').classList.add('hidden');
  const r = await api('/api/profiles', {method: 'POST', body: {name}});
  PID = r.id; localStorage.setItem('pid', PID); await loadProfiles(); showTab('profile');
  toast(`Profile "${name}" created - upload their resume`);
}

// ---------- dashboard ----------
async function refresh() {
  const [st, s] = await Promise.all([api(`/api/stats/${PID}`), api(`/api/status/${PID}`)]);
  const cards = [['New', st.new], ['Queued', (st.queued || 0) + (st.applying || 0)], ['Applied', st.applied],
    ['Need your answer', st.pending_questions], ['Failed / manual', st.failed], ['Skipped', st.skipped]];
  for (const e of st.emails) cards.push([`Emails: ${e.kind}`, e.n]);
  $('#stats').innerHTML = cards.map(([k, v]) => `<div class="stat"><b>${v || 0}</b>${k}</div>`).join('');
  $('#pendingBadge').textContent = st.pending_questions || '';
  const w = s.worker;
  $('#workerState').textContent = `Browser: ${w.busy ? w.message : 'idle'} · queue ${s.queue}` + (s.search ? ` · searching: ${s.search === true ? '…' : s.search}` : '');
  $('#logs').innerHTML = s.logs.map(l => `<div class="${l.level}">${esc(l.ts.slice(5, 19).replace('T', ' '))}  ${esc(l.msg)}</div>`).join('');
  $('#humanBar').classList.toggle('hidden', !w.waiting_human);
  $('#humanMsg').textContent = w.waiting_human ? w.message : '';
}
async function runSearch() {
  const sources = [];
  if ($('#srcAts').checked) sources.push('ats');
  if ($('#srcWeb').checked) sources.push('web');
  if ($('#srcList').checked) sources.push('listings');
  await api(`/api/search/${PID}`, {method: 'POST', body: {sources}});
  refresh();
}
async function applyAll() {
  const r = await api(`/api/jobs/${PID}/apply`, {method: 'POST', body: {all_new: true, min_score: Number($('#minScore').value), limit: Number($('#maxApply').value)}});
  toast(`${r.queued} jobs queued`); refresh();
}
async function scanEmail() { await api(`/api/email/${PID}/scan`, {method: 'POST', body: {days: 21}}); toast('Mailbox scan started - see Activity'); }
async function openBrowser() {
  const url = $('#openUrl').value.trim() || 'https://jobright.ai/jobs/recommend?from=homepage';
  const r = await api(`/api/browser/${PID}/open`, {method: 'POST', body: {url}});
  toast(r.browser === 'built-in' ? 'Opening the built-in bot browser - sign in there once'
    : `Opened in the bot's own ${r.browser.replace('.exe', '')} window - sign in there once; it stays signed in`);
}
async function stopWorker() {
  const r = await api('/api/worker/stop', {method: 'POST', body: {}});
  toast(`Stopped - ${r.cleared} queued task(s) cleared` + (r.was_busy ? ', current job will stop at its next step' : ''));
  refresh();
}
async function human(result) { await api('/api/worker/human', {method: 'POST', body: {result}}); refresh(); }

// ---------- jobs ----------
async function loadJobs() {
  const rows = await api(`/api/jobs/${PID}?status=${encodeURIComponent($('#jobStatus').value)}&q=${encodeURIComponent($('#jobQ').value)}`);
  $('#jobRows').innerHTML = rows.map(j => `<tr>
    <td><input type="checkbox" class="sel" value="${j.id}"></td>
    <td><a href="${esc(j.url)}" target="_blank">${esc(j.title)}</a></td>
    <td>${esc(j.company)}</td><td>${esc(j.location)}</td><td>${esc(j.ats)}</td>
    <td><span class="pill ${j.sponsorship}">${j.sponsorship}</span></td>
    <td>${Math.round((j.score || 0) * 100)}%</td>
    <td>${esc((j.posted_at || '').slice(0, 10))}</td>
    <td><span class="pill ${j.status}">${j.status}</span><div class="note">${esc(j.status_note || '')}</div>
      ${j.resume_used ? `<div class="note">resume: ${esc(j.resume_used.split(/[\\/]/).slice(-2).join('/'))}</div>` : ''}</td></tr>`).join('')
    || '<tr><td colspan="9" class="muted">No jobs here. Run "Find jobs" on the Dashboard.</td></tr>';
}
$('#jobStatus').onchange = loadJobs;
$('#jobQ').oninput = () => { clearTimeout(window._jq); window._jq = setTimeout(loadJobs, 300); };
$('#selAll').onchange = e => document.querySelectorAll('.sel').forEach(c => c.checked = e.target.checked);
const selected = () => [...document.querySelectorAll('.sel:checked')].map(c => Number(c.value));
async function applySelected() { const r = await api(`/api/jobs/${PID}/apply`, {method: 'POST', body: {ids: selected()}}); toast(`${r.queued} queued`); loadJobs(); }
async function markSelected(status) { await api(`/api/jobs/${PID}/status`, {method: 'POST', body: {ids: selected(), status}}); loadJobs(); }
async function addJob() {
  const url = $('#addUrl').value.trim(); if (!url) return;
  await api(`/api/jobs/${PID}/add`, {method: 'POST', body: {url, apply_now: true}});
  $('#addUrl').value = ''; $('#jobStatus').value = ''; loadJobs();
}

// ---------- pending questions ----------
async function loadPending() {
  const rows = await api(`/api/pending/${PID}`);
  $('#pendingList').innerHTML = rows.map(p => `<div class="q" data-q="${esc(p.question)}">
     <div><b>${esc(p.question)}</b> <span class="muted">(${esc(p.field_type)}${p.company ? ' · first seen at ' + esc(p.company) : ''})</span>
       <button class="ghost" style="float:right" onclick="dropPending(${p.id})">ignore</button></div>
     <div class="row">${p.field_type === 'textarea' ? '<textarea rows="3"></textarea>' : '<input style="flex:1">'}</div>
     ${p.options.length ? `<div class="opts">${p.options.slice(0, 40).map(o => `<button type="button" onclick="this.closest('.q').querySelector('input,textarea').value=this.textContent">${esc(o)}</button>`).join('')}</div>` : ''}
   </div>`).join('') || '<p class="muted">Nothing waiting. 🎉</p>';
}
async function savePending(retry) {
  const answers = [...document.querySelectorAll('#pendingList .q')].map(d => ({question: d.dataset.q, answer: d.querySelector('input,textarea').value}));
  const r = await api(`/api/pending/${PID}`, {method: 'POST', body: {answers, retry}});
  toast(r.requeued ? `Answers saved - ${r.requeued} waiting jobs re-queued` : 'Answers saved');
  loadPending(); refresh();
}
async function dropPending(id) { await api(`/api/pending/${PID}/${id}`, {method: 'DELETE'}); loadPending(); }

// ---------- profile ----------
const HELP = {
  needs_sponsorship: 'Yes / No - used for "will you require sponsorship" questions',
  work_authorization: 'e.g. F-1 STEM OPT, H-1B, US Citizen, Green Card',
  work_authorization_answer: 'Answer to "Are you legally authorized to work in the US?" (Yes / No)',
  gender: 'or "Decline to self identify"', race: 'or "Decline to self identify"',
  veteran_status: 'e.g. "I am not a protected veteran"', disability_status: 'e.g. "No, I do not have a disability"',
  target_titles: 'comma separated - also used for searching', skills: 'comma separated - used for match score',
  cover_letter_path: 'optional: full path to a cover letter file',
};
const EXTRA = ['work_authorization_answer', 'referral_source', 'cover_letter_path'];
let PROF = {};
async function loadProfile() {
  PROF = await api(`/api/profile/${PID}`);
  $('#profName').value = PROF.name;
  $('#resumeInfo').textContent = PROF.resume_path ? `Current: ${PROF.resume_path.split(/[\\/]/).slice(-2).join('/')} (${PROF.resume_chars} chars)` : 'No resume yet';
  loadResumes(); setDirty(false);
  const keys = [...new Set([...PROF.fields, ...EXTRA, ...Object.keys(PROF.data)])];
  $('#profFields').innerHTML = keys.map(k => {
    const v = PROF.data[k] ?? '';
    const big = ['skills', 'summary', 'target_titles'].includes(k);
    return `<label>${k.replace(/_/g, ' ')}${HELP[k] ? ` <span class="help">${HELP[k]}</span>` : ''}
      ${big ? `<textarea data-k="${k}" rows="3">${esc(v)}</textarea>` : `<input data-k="${k}" value="${esc(v)}">`}</label>`;
  }).join('');
}
async function loadResumes() {
  const rows = await api(`/api/profile/${PID}/resumes`);
  $('#resumeRows').innerHTML = rows.map(r => `<tr>
    <td>${esc(r.folder)}<div class="note">${esc(new Date(r.uploaded_at).toLocaleTimeString())}</div></td>
    <td>${r.exists ? `<a href="/api/profile/${PID}/resumes/${r.id}/file" target="_blank">${esc(r.filename)}</a>` : esc(r.filename) + ' <span class="note">(file missing)</span>'}</td>
    <td>${r.used_in} application${r.used_in === 1 ? '' : 's'}</td>
    <td>${r.active ? '<span class="pill applied">in use</span>' : r.exists ? `<button class="ghost" onclick="useResume(${r.id})">Use this one</button>` : ''}</td></tr>`).join('')
    || '<tr><td colspan="4" class="muted">No resumes uploaded yet.</td></tr>';
}
async function useResume(id) { await api(`/api/profile/${PID}/resumes/${id}/use`, {method: 'POST'}); loadProfile(); }
function addField() {
  const k = $('#newFieldKey').value.trim().toLowerCase().replace(/\W+/g, '_'); if (!k) return;
  PROF.data[k] = ''; $('#newFieldKey').value = ''; saveProfile().then(loadProfile);
}
async function saveProfile() {
  const data = {};
  document.querySelectorAll('#profFields [data-k]').forEach(i => { if (i.value.trim() || PROF.data[i.dataset.k] !== undefined) data[i.dataset.k] = i.value.trim(); });
  Object.keys(PROF.data).forEach(k => { if (!(k in data)) data[k] = PROF.data[k]; });
  await api(`/api/profile/${PID}`, {method: 'PUT', body: {name: $('#profName').value, data}});
  PROF.data = data; setDirty(false);
  await loadProfiles();
  toast('Profile saved');
}
function setDirty(d) { $('#profDirty').textContent = d ? 'Unsaved changes' : ''; }
$('#profile').addEventListener('input', e => { if (e.target.closest('#profFields') || e.target.id === 'profName') setDirty(true); });
document.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === 's' && $('#profile').classList.contains('active')) { e.preventDefault(); saveProfile(); } });
async function uploadResume() {
  const f = $('#resumeFile').files[0]; if (!f) return toast('Choose a resume file first', 'bad');
  const fd = new FormData(); fd.append('file', f);
  $('#resumeInfo').textContent = 'Parsing…';
  const r = await fetch(`/api/profile/${PID}/resume`, {method: 'POST', body: fd});
  if (!r.ok) { $('#resumeInfo').textContent = ''; return toast(errText(await r.text()), 'bad'); }
  toast('Resume uploaded and parsed');
  loadProfile();
}

// ---------- answers ----------
async function loadAnswers() {
  const rows = await api(`/api/answers/${PID}`);
  const f = $('#ansFilter').value.toLowerCase();
  $('#ansRows').innerHTML = rows.filter(r => !f || (r.question + r.answer).toLowerCase().includes(f)).map(r => `<tr>
    <td>${esc(r.question)}</td>
    <td><textarea rows="1" onchange="updAnswer(${r.id}, this.value)">${esc(r.answer)}</textarea></td>
    <td><span class="pill">${r.source === 'llm' ? 'AI (check it)' : r.source}</span></td>
    <td><button class="ghost" onclick="delAnswer(${r.id})">delete</button></td></tr>`).join('')
    || '<tr><td colspan="4" class="muted">No saved answers yet. They appear as the bot learns.</td></tr>';
}
$('#ansFilter').oninput = loadAnswers;
async function addAnswer() { await api(`/api/answers/${PID}`, {method: 'POST', body: {question: $('#ansQ').value, answer: $('#ansA').value}}); $('#ansQ').value = $('#ansA').value = ''; loadAnswers(); }
async function updAnswer(id, answer) { await api(`/api/answers/${PID}`, {method: 'POST', body: {id, answer}}); }
async function delAnswer(id) { await api(`/api/answers/${PID}/${id}`, {method: 'DELETE'}); loadAnswers(); }

// ---------- avoid ----------
async function loadAvoid() {
  const list = await api(`/api/avoid/${PID}`);
  $('#avoidList').innerHTML = list.map(c => `<span class="chip">${esc(c)}<button onclick="delAvoid('${esc(c).replace(/'/g, "\\'")}')">×</button></span>`).join('') || '<span class="muted">Empty</span>';
}
async function addAvoid() { await api(`/api/avoid/${PID}`, {method: 'POST', body: {companies: $('#avoidInput').value}}); $('#avoidInput').value = ''; loadAvoid(); }
async function delAvoid(c) { await api(`/api/avoid/${PID}?company=${encodeURIComponent(c)}`, {method: 'DELETE'}); loadAvoid(); }

// ---------- emails ----------
async function loadEmails() {
  const rows = await api(`/api/emails/${PID}`);
  $('#emailRows').innerHTML = rows.map(e => `<tr><td>${esc(e.received_at.slice(0, 10))}</td><td><span class="pill">${e.kind}</span></td>
    <td>${esc(e.company)}</td><td>${esc(e.subject)}</td><td>${esc(e.title || '')}</td></tr>`).join('')
    || '<tr><td colspan="5" class="muted">No emails scanned yet (set up IMAP in Settings).</td></tr>';
}

// ---------- settings ----------
const SET_HELP = {
  anthropic_api_key: 'Claude API key - parses resumes and answers new questions (optional but recommended)',
  llm_model: 'Claude model id', auto_submit: 'true = submit automatically; false = fill and wait for you to click Submit',
  headless: 'false = you can watch the browser (needed for logins/CAPTCHAs)',
  chrome_path: 'leave empty to auto-detect Chrome/Edge; otherwise the full path to chrome.exe',
  needs_sponsorship: 'true = skip postings that say they do not sponsor visas',
  max_job_age_days: 'only jobs posted within N days', min_score: 'default minimum match score (0-1)',
  imap_host: 'e.g. imap.gmail.com / outlook.office365.com', imap_user: 'mailbox used on applications',
  imap_password: 'Gmail: create an App Password (Google Account → Security → App passwords)',
  ats_account_password: 'password the bot uses to sign in / create accounts on Workday and similar portals',
  search_keywords: 'job titles to search, comma separated', search_locations: 'comma separated (e.g. United States, Remote, Texas)',
  exclude_title_words: 'skip titles containing these words', brave_api_key: 'optional: Brave Search API key for better web search',
  serper_api_key: 'optional: serper.dev key (Google results) for better web search',
  greenhouse_boards: 'extra Greenhouse board tokens (boards.greenhouse.io/<token>)', lever_companies: 'extra Lever company ids',
  ashby_orgs: 'extra Ashby org ids', workday_sites: 'tenant|wdN|site e.g. nvidia|wd5|NVIDIAExternalCareerSite',
  listing_urls: 'one per line: jobright.ai / LinkedIn / Built In search pages to scrape (log in once via Open bot browser)',
};
async function loadSettings() {
  const s = await api(`/api/settings/${PID}`);
  $('#settingsForm').innerHTML = Object.entries(s).map(([k, v]) => {
    const secret = /key|password/.test(k);
    const input = k === 'listing_urls' ? `<textarea data-k="${k}" rows="3">${esc(v)}</textarea>`
      : `<input data-k="${k}" ${secret ? 'type="password" placeholder="' + (v === '__SET__' ? '(saved - leave blank to keep)' : '') + '"' : ''} value="${secret ? '' : esc(v)}">`;
    return `<label>${k.replace(/_/g, ' ')} <span class="help">${SET_HELP[k] || ''}</span>${input}</label>`;
  }).join('');
}
async function saveSettings() {
  const body = {};
  document.querySelectorAll('#settingsForm [data-k]').forEach(i => {
    if (i.type === 'password' && !i.value) return;
    body[i.dataset.k] = i.value;
  });
  await api(`/api/settings/${PID}`, {method: 'PUT', body}); toast('Settings saved'); loadSettings();
}

// ---------- boot ----------
(async () => {
  await loadProfiles(); localStorage.setItem('pid', PID);
  refresh();
  setInterval(() => { if ($('#dash').classList.contains('active') || !$('#humanBar').classList.contains('hidden')) refresh(); else api(`/api/status/${PID}`).then(s => { $('#humanBar').classList.toggle('hidden', !s.worker.waiting_human); $('#humanMsg').textContent = s.worker.message; }); }, 3000);
})();
