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
const errText = t => { try { const j = JSON.parse(t); return j.detail || j.error || j.message || t; } catch { return t; } };
const ls = {get: k => { try { return localStorage.getItem(k) || ''; } catch { return ''; } },
            set: (k, v) => { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch {} }};
// ONLINE = true when this page is served by the Netlify site (the bot itself runs on your PC)
let ONLINE = false, WORKER_SEEN = 0;
const authHeaders = () => ONLINE ? {'x-app-password': ls.get('jobbot_pw')} : {};
class Queued extends Error {}
window.addEventListener('unhandledrejection', e => { if (e.reason instanceof Queued) e.preventDefault(); });

function setOffline(off) {
  const bar = $('#offlineBar'); if (!bar) return;
  const was = !bar.classList.contains('hidden');
  bar.classList.toggle('hidden', !off);
  if (was && !off) { toast('Reconnected to the bot'); showTab(document.querySelector('#tabs button.active')?.dataset.tab || 'dash'); }
}
async function api(path, opts = {}) {
  let r;
  const isForm = opts.body instanceof FormData;
  try {
    r = await fetch(path, {...opts, headers: {...(isForm ? {} : {'Content-Type': 'application/json'}), ...authHeaders()},
      body: opts.body && typeof opts.body !== 'string' && !isForm ? JSON.stringify(opts.body) : opts.body});
  } catch (e) { setOffline(true); throw e; }
  if (!ONLINE) setOffline(false);
  if (r.status === 401 && ONLINE) { showLogin('Please enter your password.'); throw new Queued('login'); }
  if (r.status === 202) {           // online: the PC worker didn't answer in time; the change is queued
    const j = await r.json().catch(() => ({}));
    toast(j.message || 'Sent to your PC - it runs when the PC worker is online');
    throw new Queued('queued');
  }
  if (!r.ok) { const t = await r.text(); toast(errText(t), 'bad'); throw new Error(t); }
  return r.json();
}

// ---------- online: password + PC worker status ----------
function showLogin(msg) {
  let box = $('#loginBox');
  if (!box) {
    box = document.createElement('div'); box.id = 'loginBox'; box.className = 'login';
    box.innerHTML = `<form class="card"><h3>Job Bot</h3><p class="muted" id="loginMsg"></p>
      <input type="password" id="loginPw" placeholder="Password (APP_PASSWORD on Netlify)" autocomplete="current-password">
      <button>Sign in</button></form>`;
    document.body.appendChild(box);
    box.querySelector('form').onsubmit = async e => {
      e.preventDefault();
      const pw = $('#loginPw').value;
      const s = await fetch('/api/_auth', {headers: {'x-app-password': pw}}).then(r => r.json()).catch(() => ({}));
      if (s.authed) { ls.set('jobbot_pw', pw); box.remove(); booted = false; boot(); }
      else $('#loginMsg').textContent = 'Wrong password.';
    };
  }
  $('#loginMsg').textContent = msg || '';
  $('#loginPw').focus();
}
async function checkMeta() {
  const m = await fetch('/api/_meta', {headers: authHeaders()}).then(r => r.ok ? r.json() : null).catch(() => null);
  if (!m) return;
  ONLINE = !!m.online;
  const badge = $('#workerBadge');
  if (!ONLINE) {                     // on the PC: show whether the online dashboard is connected
    const r = m.relay || {};
    badge.textContent = r.connected ? 'Online dashboard: connected' : '';
    badge.className = 'badge-state ok';
    return;
  }
  WORKER_SEEN = m.worker_last_seen;
  const ago = Math.round((m.now - m.worker_last_seen) / 1000);
  const up = m.worker_last_seen && ago < 90;
  badge.textContent = up ? 'PC worker: online' : (m.worker_last_seen ? `PC worker: offline (seen ${fmtAgo(ago)} ago)` : 'PC worker: never connected');
  badge.className = 'badge-state ' + (up ? 'ok' : 'bad');
  $('#offlineBar').classList.toggle('hidden', up);
  $('#offlineBar').innerHTML = up ? '' : `Your PC worker is offline${m.worker_last_seen ? ` (last seen ${fmtAgo(ago)} ago)` : ''}.
    You can still look around and make changes; they are queued and run when you start <b>start.bat</b> on your PC.`;
}
const fmtAgo = s => s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} days`;

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
let SETUP = null;
async function refresh() {
  const [st, s] = await Promise.all([api(`/api/stats/${PID}`), api(`/api/status/${PID}`)]);
  const cards = [['New jobs found', st.new, 'jobs:new'], ['Applying now', (st.queued || 0) + (st.applying || 0), 'jobs:queued,applying'],
    ['Applied', st.applied, 'jobs:applied'], ['Questions for you', st.pending_questions, 'pending'],
    ["Couldn't finish", st.failed, 'jobs:failed'], ['Skipped', st.skipped, 'jobs:skipped']];
  const em = Object.fromEntries(st.emails.map(e => [e.kind, e.n]));
  if (em.interview) cards.push(['Interview emails', em.interview, 'emails']);
  if (em.rejected) cards.push(['Rejections', em.rejected, 'emails']);
  $('#stats').innerHTML = cards.map(([k, v, go]) =>
    `<button class="stat${k === 'Questions for you' && v ? ' alert' : ''}" onclick="goTo('${go}')"><b>${v || 0}</b>${k}</button>`).join('');
  $('#pendingBadge').textContent = st.pending_questions || '';
  const w = s.worker;
  let line = s.paused_for ? 'Paused while you sign in' : w.busy ? friendlyTask(w) : s.search ? 'Searching company career pages…' : 'Idle';
  if (s.queue) line += ` · ${s.queue} more waiting`;
  $('#workerState').textContent = 'Bot: ' + line;
  $('#workerState').className = 'state-line ' + (w.busy || s.search ? 'busy' : s.paused_for ? 'paused' : '');
  $('#pausedNote').classList.toggle('hidden', !s.paused_for);
  $('#pausedLeft').textContent = `${Math.ceil(s.paused_for / 60)} min`;
  $('#logs').innerHTML = s.logs.map(l => `<div class="${l.level}">${esc(new Date(l.ts).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}))}  ${esc(l.msg)}</div>`).join('');
  $('#humanBar').classList.toggle('hidden', !w.waiting_human);
  $('#humanMsg').textContent = w.waiting_human ? w.message : '';
  if (!SETUP || Date.now() - SETUP._at > 20000) loadSetup();
}
function friendlyTask(w) {
  const [kind] = w.current || [];
  if (kind === 'apply') return 'Applying to a job…';
  if (kind === 'listing') return 'Reading job sites…';
  if (kind === 'open') return 'Opening Chrome…';
  return w.message;
}
function goTo(where) {
  const [tab, status] = where.split(':');
  if (status !== undefined) $('#jobStatus').value = status;
  showTab(tab);
}
async function loadSetup() {
  SETUP = await api(`/api/setup/${PID}`); SETUP._at = Date.now();
  const req = SETUP.steps.filter(x => x.required), done = req.filter(x => x.done).length;
  $('#setupProgress').textContent = SETUP.ready ? 'All set - the bot is ready to apply.' : `${done} of ${req.length} required steps done`;
  $('#setupSteps').innerHTML = SETUP.steps.map(x => `<li class="${x.done ? 'done' : ''}">
      <span class="tick">${x.done ? '✓' : ''}</span>
      <div><b>${esc(x.title)}</b>${x.required ? '' : ' <span class="opt">optional</span>'}<div class="muted">${esc(x.detail)}</div></div>
      ${x.done ? '' : `<button class="ghost" onclick="goSetup('${x.tab}')">${x.tab === 'signin' ? 'Sign in' : 'Go'}</button>`}</li>`).join('');
  const hidden = ls.get('setupHidden') === '1' && SETUP.ready;
  $('#setupSteps').classList.toggle('hidden', hidden);
  $('#setupToggle').textContent = hidden ? 'Show' : 'Hide';
  $('#siteButtons').innerHTML = SETUP.sites.filter(x => x.enabled).map(x =>
    `<button class="site ${x.signed_in ? 'ok' : ''}" onclick="signin('${x.id}')">${esc(x.label)}${x.signed_in ? ' ✓' : ''}</button>`).join('');
}
function toggleSetup() {
  const hide = !$('#setupSteps').classList.contains('hidden');
  $('#setupSteps').classList.toggle('hidden', hide); ls.set('setupHidden', hide ? '1' : '');
  $('#setupToggle').textContent = hide ? 'Show' : 'Hide';
}
function goSetup(tab) {
  if (tab === 'signin') return $('#signinCard').scrollIntoView({behavior: 'smooth'});
  showTab(tab);
}
const SIGNING = new Set();
async function signin(site) {
  await api(`/api/signin/${PID}`, {method: 'POST', body: {site}});
  SIGNING.add(site);
  toast(`Opened ${SETUP.sites.find(x => x.id === site).label} in the bot's Chrome window. Sign in there, then click "I'm done signing in".`);
  refresh();
}
async function signinDone() {
  await api(`/api/signin/${PID}/done`, {method: 'POST', body: {sites: [...SIGNING]}});
  SIGNING.clear(); SETUP = null;
  toast('Thanks - the bot is running again'); refresh();
}
async function openOther() {
  const url = $('#openUrl').value.trim(); if (!url) return toast('Type a web address first', 'bad');
  await api(`/api/signin/${PID}`, {method: 'POST', body: {url}});
  toast(`Opened in the bot's Chrome window. The bot is paused until you click "I'm done signing in".`); refresh();
}
async function runSearch() {
  const sources = [];
  if ($('#srcAts').checked) sources.push('ats');
  if ($('#srcWeb').checked) sources.push('web');
  if ($('#srcList').checked) sources.push('listings');
  if (!sources.length) return toast('Pick at least one place to search', 'bad');
  await api(`/api/search/${PID}`, {method: 'POST', body: {sources}});
  toast('Searching - new jobs appear in the Jobs tab as they are found');
  refresh();
}
async function applyAll() {
  const r = await api(`/api/jobs/${PID}/apply`, {method: 'POST', body: {all_new: true, min_score: Number($('#minScore').value) / 100, limit: Number($('#maxApply').value)}});
  toast(r.queued ? `Applying to ${r.queued} job${r.queued === 1 ? '' : 's'} - watch progress in the Jobs tab`
    : 'No new jobs match yet - run Find jobs first, or lower the skill match');
  refresh();
}
async function scanEmail() { await api(`/api/email/${PID}/scan`, {method: 'POST', body: {days: 21}}); toast('Reading your mailbox - results appear in the Emails tab'); }
async function stopWorker() {
  const r = await api('/api/worker/stop', {method: 'POST', body: {}});
  toast(`Stopped${r.cleared ? ` - ${r.cleared} waiting task(s) cancelled` : ''}` + (r.was_busy ? '; the current job stops at its next step' : ''));
  refresh();
}
async function human(result) { await api('/api/worker/human', {method: 'POST', body: {result}}); refresh(); }

// ---------- jobs ----------
const STATUS = {new: 'New', queued: 'Waiting to apply', applying: 'Applying now', needs_input: 'Needs your answer',
  needs_human: 'Needs you in Chrome', applied: 'Applied', failed: "Couldn't finish", skipped: 'Skipped'};
const VISA = {yes: ['Sponsors', 'yes'], no: ['No sponsorship', 'no'], unknown: ['Not stated', 'unknown']};
const SITE = {greenhouse: 'Greenhouse', lever: 'Lever', ashby: 'Ashby', workday: 'Workday', linkedin: 'LinkedIn', dice: 'Dice',
  ziprecruiter: 'ZipRecruiter', monster: 'Monster', jobright: 'jobright', icims: 'iCIMS', smartrecruiters: 'SmartRecruiters',
  other: 'Company site'};
async function loadJobs() {
  const rows = await api(`/api/jobs/${PID}?status=${encodeURIComponent($('#jobStatus').value)}&q=${encodeURIComponent($('#jobQ').value)}`);
  $('#jobRows').innerHTML = rows.map(j => {
    const [vl, vc] = VISA[j.sponsorship] || VISA.unknown;
    const canApply = ['new', 'failed', 'skipped', 'needs_input'].includes(j.status);
    const actions = canApply ? `<button onclick="applyOne(${j.id})">Apply</button>` +
      (j.status !== 'skipped' ? `<button class="ghost" onclick="markOne(${j.id}, 'skipped')">Skip</button>` : '') : '';
    const link = j.url && !j.url.startsWith('email:');
    return `<tr>
    <td><input type="checkbox" class="sel" value="${j.id}"></td>
    <td>${link ? `<a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>` : esc(j.title)}</td>
    <td>${esc(j.company)}</td><td>${esc(j.location)}</td><td>${esc(SITE[j.ats] || j.ats || '')}</td>
    <td><span class="pill ${vc}">${vl}</span></td>
    <td>${j.score ? Math.round(j.score * 100) + '%' : ''}</td>
    <td>${esc((j.posted_at || j.applied_at || '').slice(0, 10))}</td>
    <td><span class="pill ${j.status}">${STATUS[j.status] || j.status}</span><div class="note">${esc(j.status_note || '')}</div>
      ${j.resume_used ? `<div class="note">resume: ${esc(j.resume_used.split(/[\\/]/).slice(-2).join('/'))}</div>` : ''}</td>
    <td class="actions">${actions}</td></tr>`;
  }).join('') || `<tr><td colspan="10" class="muted">${$('#jobStatus').value === 'new' ? 'No new jobs yet. Click "Find jobs" on the Dashboard.' : 'Nothing here.'}</td></tr>`;
}
async function applyOne(id) { await api(`/api/jobs/${PID}/apply`, {method: 'POST', body: {ids: [id]}}); toast('Applying - this row updates as it goes'); loadJobs(); }
async function markOne(id, status) { await api(`/api/jobs/${PID}/status`, {method: 'POST', body: {ids: [id], status}}); loadJobs(); }

// ---------- pending questions ----------
async function loadPending() {
  const rows = await api(`/api/pending/${PID}`);
  $('#pendingList').innerHTML = rows.map(p => `<div class="q" data-q="${esc(p.question)}">
     <div><b>${esc(p.question)}</b> ${p.company ? `<span class="muted"> · asked by ${esc(p.company)}</span>` : ''}
       <button class="ghost" style="float:right" onclick="dropPending(${p.id})" title="Remove without answering">Skip question</button></div>
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

// ---------- form helpers (plain-language labels, groups, Yes/No pickers) ----------
const YESNO = ['Yes', 'No'];
const DECLINE = 'Decline to self identify';
// field: [label, help, options (dropdown) or 'long' (text area)]
const PROFILE_GROUPS = [
  ['Contact', {first_name: ['First name'], last_name: ['Last name'], full_name: ['Full name'], email: ['Email'],
    phone: ['Phone'], linkedin: ['LinkedIn URL'], github: ['GitHub URL'], website: ['Website / portfolio']}],
  ['Where you live', {address: ['Street address'], city: ['City'], state: ['State'], zip: ['ZIP code'],
    country: ['Country'], willing_to_relocate: ['Willing to relocate?', '', YESNO]}],
  ['Work authorization', {
    work_authorization_answer: ['Are you legally authorized to work in the US?', 'Your answer on application forms', YESNO],
    needs_sponsorship: ['Will you need visa sponsorship now or in the future?', 'Your answer on application forms', YESNO],
    work_authorization: ['Your status', 'e.g. F-1 STEM OPT, H-1B, Green Card, US Citizen'],
    visa_status: ['Visa type', 'e.g. OPT, H-1B']}],
  ['Experience & education', {current_title: ['Current / last job title'], current_company: ['Current / last employer'],
    years_experience: ['Years of experience'], school: ['School / university'], degree: ['Degree'], major: ['Major'],
    graduation_year: ['Graduation year']}],
  ['What you want', {target_titles: ['Job titles you want', 'Comma separated - also used to search', 'long'],
    desired_salary: ['Desired salary', 'e.g. 120000'], notice_period: ['Notice period / start date', 'e.g. 2 weeks'],
    referral_source: ['"How did you hear about us?" answer', 'e.g. LinkedIn'],
    cover_letter_path: ['Cover letter file (optional)', 'Full path to a PDF/DOCX on your PC']}],
  ['Voluntary self-identification (optional)', {
    gender: ['Gender', '', ['Male', 'Female', 'Non-binary', DECLINE]],
    race: ['Race / ethnicity', '', ['Asian', 'Black or African American', 'Hispanic or Latino', 'White',
      'Native Hawaiian or Other Pacific Islander', 'American Indian or Alaska Native', 'Two or More Races', DECLINE]],
    veteran_status: ['Veteran status', '', ['I am not a protected veteran', 'I identify as one or more of the classifications of protected veteran', DECLINE]],
    disability_status: ['Disability', '', ['No, I do not have a disability', 'Yes, I have a disability (or previously had a disability)', DECLINE]],
    pronouns: ['Pronouns']}],
  ['About you', {skills: ['Skills', 'Comma separated - used for the match score', 'long'], summary: ['Summary', '', 'long']}],
];
const SETTING_GROUPS = [
  ['What jobs to look for', {
    search_keywords: ['Job titles to search', 'Comma separated, e.g. Software Engineer, Java Developer', 'long'],
    search_locations: ['Locations', 'Comma separated, e.g. United States, Remote, Texas'],
    max_job_age_days: ['Only jobs posted in the last … days'],
    needs_sponsorship: ['Skip jobs that say they do not sponsor visas', '', 'bool'],
    exclude_title_words: ['Skip titles containing', 'e.g. intern, manager, principal', 'long'],
    job_sites: ['Job sites to search', '', 'sites']}],
  ['Applying', {
    auto_submit: ['Submit applications automatically', 'Off = the bot fills the form and you click Submit', 'bool'],
    max_per_site_per_day: ['Most applications per job site per day', 'Keeps your LinkedIn/Dice accounts safe. 0 = no limit'],
    min_score: ['Minimum skill match (0 to 1)', 'Default for "Apply to new matches"'],
    ats_account_password: ['Password for Workday-style accounts', 'The bot uses it to create/sign in to company career accounts', 'secret']}],
  ['Email', {
    imap_user: ['Gmail address', 'The email you use on applications'],
    imap_password: ['Gmail app password', 'Google Account > Security > App passwords (16 letters, no spaces)', 'secret'],
    imap_host: ['Mail server', 'imap.gmail.com for Gmail, outlook.office365.com for Outlook']}],
  ['Use it from your phone', {
    online_url: ['Online dashboard address', 'Your Netlify site, e.g. https://quiet-blini-fd7a6c.netlify.app', 'pc'],
    online_password: ['Online dashboard password', 'The APP_PASSWORD you set on Netlify', 'secret']}],
  ['AI and search (optional)', {
    anthropic_api_key: ['Claude API key', 'Lets the bot answer new questions from your resume', 'secret'],
    llm_model: ['Claude model'],
    serper_api_key: ['Serper.dev key', 'Better internet job search (Google results)', 'secret'],
    brave_api_key: ['Brave Search key', 'Alternative to Serper', 'secret']}],
  ['Advanced', {
    headless: ['Hide the bot\'s Chrome window', 'Keep off: you need the window to sign in and pass robot checks', 'bool'],
    chrome_path: ['Chrome location', 'Leave empty to find Chrome/Edge automatically', 'pc'],
    greenhouse_boards: ['Extra Greenhouse companies', 'Board names, comma separated'],
    lever_companies: ['Extra Lever companies'], ashby_orgs: ['Extra Ashby companies'],
    workday_sites: ['Extra Workday sites', 'tenant|wdN|site, e.g. nvidia|wd5|NVIDIAExternalCareerSite'],
    listing_urls: ['Extra search pages to read', 'One web address per line, e.g. a saved LinkedIn search', 'long']}],
];
const SITE_NAMES = {jobright: 'jobright.ai', linkedin: 'LinkedIn', dice: 'Dice', ziprecruiter: 'ZipRecruiter', monster: 'Monster'};

function fieldHtml(k, v, [label, help = '', kind = ''], opts = {}) {
  v = v ?? '';
  let input;
  if (Array.isArray(kind)) {
    const known = kind.some(o => o.toLowerCase() === String(v).toLowerCase());
    input = `<select data-k="${k}"><option value="">-</option>${kind.map(o => `<option ${o.toLowerCase() === String(v).toLowerCase() ? 'selected' : ''}>${esc(o)}</option>`).join('')}
      ${v && !known ? `<option selected>${esc(v)}</option>` : ''}</select>`;
  } else if (kind === 'long') input = `<textarea data-k="${k}" rows="2">${esc(v)}</textarea>`;
  else if (kind === 'bool') input = `<label class="switch"><input type="checkbox" data-k="${k}" data-bool="1" ${String(v).toLowerCase() === 'true' ? 'checked' : ''}><span>${String(v).toLowerCase() === 'true' ? 'On' : 'Off'}</span></label>`;
  else if (kind === 'sites') {
    const on = String(v).split(',').map(x => x.trim());
    input = `<div class="checks" data-k="${k}" data-sites="1">${Object.entries(SITE_NAMES).map(([id, n]) =>
      `<label><input type="checkbox" value="${id}" ${on.includes(id) ? 'checked' : ''}> ${n}</label>`).join('')}</div>`;
  } else if (kind === 'secret') {
    if (opts.online) input = `<input disabled value="${v === '__SET__' ? 'saved on your PC' : 'not set'}">`;
    else input = `<input type="password" data-k="${k}" placeholder="${v === '__SET__' ? 'Saved - type a new one to change it' : 'Not set'}" autocomplete="new-password">`;
  } else if (kind === 'pc' && opts.online) input = `<input disabled value="${esc(v)}">`;
  else input = `<input data-k="${k}" value="${esc(v)}">`;
  const note = opts.online && (kind === 'secret' || kind === 'pc') ? 'Change this on your PC - it never leaves your PC.' : help;
  return `<label class="field"><span class="lbl">${esc(label)}</span>${input}${note ? `<span class="help">${esc(note)}</span>` : ''}</label>`;
}
function groupsHtml(groups, values, opts) {
  const used = new Set();
  const html = groups.map(([title, fields], i) => {
    Object.keys(fields).forEach(k => used.add(k));
    const advanced = title === 'Advanced';
    const inner = `<div class="fields">${Object.entries(fields).map(([k, f]) => fieldHtml(k, values[k], f, opts)).join('')}</div>`;
    return advanced ? `<details class="card group"><summary><h3>${title}</h3></summary>${inner}</details>`
                    : `<div class="card group"><h3>${title}</h3>${inner}</div>`;
  }).join('');
  return {html, used};
}
function readFields(root) {
  const out = {};
  root.querySelectorAll('[data-k]').forEach(el => {
    const k = el.dataset.k;
    if (el.dataset.sites) out[k] = [...el.querySelectorAll('input:checked')].map(i => i.value).join(', ');
    else if (el.dataset.bool) out[k] = el.checked ? 'true' : 'false';
    else if (el.type === 'password') { if (el.value) out[k] = el.value; }
    else out[k] = el.value.trim();
  });
  return out;
}
document.addEventListener('change', e => {
  if (e.target.dataset && e.target.dataset.bool) e.target.nextElementSibling.textContent = e.target.checked ? 'On' : 'Off';
});

// ---------- profile ----------
let PROF = {};
async function loadProfile() {
  PROF = await api(`/api/profile/${PID}`);
  $('#profName').value = PROF.name;
  $('#resumeInfo').textContent = PROF.resume_path ? `Current: ${PROF.resume_path.split(/[\\/]/).slice(-2).join('/')} (${PROF.resume_chars} chars)` : 'No resume yet';
  loadResumes(); setDirty(false);
  const {html, used} = groupsHtml(PROFILE_GROUPS, PROF.data, {});
  const extra = Object.keys(PROF.data).filter(k => !used.has(k) && !['work_authorization_answer'].includes(k));
  $('#profFields').innerHTML = html + (extra.length ? `<div class="card group"><h3>Other saved details</h3><div class="fields">${
    extra.map(k => fieldHtml(k, PROF.data[k], [k.replace(/_/g, ' ')])).join('')}</div></div>` : '');
}
async function loadResumes() {
  const rows = await api(`/api/profile/${PID}/resumes`);
  $('#resumeRows').innerHTML = rows.map(r => `<tr>
    <td>${esc(r.folder)}<div class="note">${esc(new Date(r.uploaded_at).toLocaleTimeString())}</div></td>
    <td>${r.exists ? `<a href="#" onclick="downloadResume(${r.id}, this.textContent); return false">${esc(r.filename)}</a>` : esc(r.filename) + ' <span class="note">(file missing)</span>'}</td>
    <td>${r.used_in} application${r.used_in === 1 ? '' : 's'}</td>
    <td>${r.active ? '<span class="pill applied">in use</span>' : r.exists ? `<button class="ghost" onclick="useResume(${r.id})">Use this one</button>` : ''}</td></tr>`).join('')
    || '<tr><td colspan="4" class="muted">No resumes uploaded yet.</td></tr>';
}
async function downloadResume(id, name) {
  const r = await fetch(`/api/profile/${PID}/resumes/${id}/file`, {headers: authHeaders()});
  if (!r.ok) return toast(ONLINE ? 'Your PC worker must be online to download a resume' : 'File not found', 'bad');
  const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob()); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
async function useResume(id) { await api(`/api/profile/${PID}/resumes/${id}/use`, {method: 'POST'}); loadProfile(); }
function addField() {
  const k = $('#newFieldKey').value.trim().toLowerCase().replace(/\W+/g, '_'); if (!k) return;
  PROF.data[k] = ''; $('#newFieldKey').value = ''; saveProfile().then(loadProfile);
}
async function saveProfile() {
  const data = {...PROF.data, ...readFields($('#profFields'))};
  for (const k of Object.keys(data)) if (data[k] === '' && PROF.data[k] === undefined) delete data[k];
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
  try { await api(`/api/profile/${PID}/resume`, {method: 'POST', body: fd}); }
  catch (e) { $('#resumeInfo').textContent = e instanceof Queued ? 'Sent to your PC - it will be parsed there' : ''; return; }
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
async function loadSettings() {
  const s = await api(`/api/settings/${PID}`);
  const {html} = groupsHtml(SETTING_GROUPS, s, {online: ONLINE});
  $('#settingsForm').innerHTML = html;
  $('#setDirty').textContent = '';
}
$('#settingsForm').addEventListener('input', () => { $('#setDirty').textContent = 'Unsaved changes'; });
$('#settingsForm').addEventListener('change', () => { $('#setDirty').textContent = 'Unsaved changes'; });
async function saveSettings() {
  await api(`/api/settings/${PID}`, {method: 'PUT', body: readFields($('#settingsForm'))});
  toast('Settings saved'); SETUP = null; loadSettings();
}

// ---------- boot ----------
let booted = false, lastMeta = 0;
async function boot() {
  try {
    // /api/_auth only exists on the online (Netlify) site
    const a = await fetch('/api/_auth', {headers: {'x-app-password': ls.get('jobbot_pw')}})
      .then(r => r.ok ? r.json() : null).catch(() => null);
    if (a && 'authed' in a) {
      ONLINE = true;
      if (!a.authed) return showLogin(ls.get('jobbot_pw') ? 'Wrong password.' : '');
    }
    await checkMeta(); lastMeta = Date.now();
    await loadProfiles(); localStorage.setItem('pid', PID);
    booted = true;
    refresh().catch(() => {});
  } catch { /* bot not running yet: the offline bar is showing; retry below */ }
}
async function poll() {
  if (!booted) return $('#loginBox') ? null : boot();
  if (Date.now() - lastMeta > 15000) { lastMeta = Date.now(); checkMeta(); }
  if ($('#dash').classList.contains('active') || !$('#humanBar').classList.contains('hidden')) return refresh();
  const s = await api(`/api/status/${PID}`);
  $('#humanBar').classList.toggle('hidden', !s.worker.waiting_human);
  $('#humanMsg').textContent = s.worker.message;
}
boot();
setInterval(() => poll().catch(() => {}), 3000);
if ('serviceWorker' in navigator && location.protocol === 'https:')
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
