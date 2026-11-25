// Shared helpers. All DB/user text goes into the DOM through textContent, never innerHTML.
// The Twitch player refuses to load on a plain-http page, so a public http visit moves to https
// (the Cloudflare and Tailscale addresses both serve https; local addresses are left alone).
if (location.protocol === 'http:' && !location.port && !/^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.|\[?::1)/.test(location.hostname))
  location.replace('https://' + location.host + location.pathname + location.search + location.hash);
const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c !== null && c !== undefined) node.append(c);
  return node;
}

function fmtTime(s) {
  s = Math.max(0, Math.floor(s));
  const p = n => String(n).padStart(2, '0');
  return `${p(Math.floor(s / 3600))}:${p(Math.floor(s / 60) % 60)}:${p(s % 60)}`;
}
function fmtDate(d) { return d && d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : (d || ''); }
function watchUrl(id, t) { return `watch.html?v=${encodeURIComponent(id)}&t=${Math.floor(t || 0)}`; }

// "[[hit]] text" -> text nodes and <mark> elements
function snippetNodes(s) {
  const out = [];
  for (const part of String(s || '').split(/(\[\[.*?\]\])/s)) {
    if (part.startsWith('[[') && part.endsWith(']]')) out.push(el('mark', { text: part.slice(2, -2) }));
    else if (part) out.push(document.createTextNode(part));
  }
  return out;
}

// A mod-deleted chat message: hidden behind "deleted by mods · tap to show"; a click shows the text.
// `nodes` is what to show once opened (defaults to the message text). If the text was not published
// (only the placeholder came back), there is nothing to reveal.
const revealable = m => !!(m.deleted && m.kind !== 'mod_action' && m.text && !/^\[deleted by mods\]$/i.test(m.text.trim())
  && !(CONFIG && CONFIG.mode === 'admin'));
const REVEALED = new Set();   // ids opened this visit: the chat panes re-render often, opened stays opened
function deletedBody(m, nodes) {
  if (!revealable(m)) return el('span', { class: 'body', text: m.text || '[deleted by mods]' });
  const text = m.text;
  if (m.id !== undefined && REVEALED.has(m.id))
    return el('span', { class: 'body revealed' }, ...(nodes || [document.createTextNode(text)]));
  const b = el('button', { type: 'button', class: 'reveal', title: 'Show what the message said',
    text: 'deleted by mods · tap to show' });
  b.addEventListener('click', e => {
    e.stopPropagation();
    if (m.id !== undefined) REVEALED.add(m.id);
    b.replaceWith(el('span', { class: 'body revealed' }, ...(nodes || [document.createTextNode(text)])));
  });
  return b;
}

// Twitch deletes past broadcasts about 60 days after the stream; its embed then only says "Failed to determine
// content classification". Older Twitch streams start on the transcript-and-chat clock instead.
// upload_date is YYYYMMDD.
function twitchGone(embedType, uploadDate) {
  const d = String(uploadDate || '');
  if (embedType !== 'twitch' || d.length !== 8) return false;
  return (Date.now() - Date.UTC(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8))) / 864e5 > 61;
}

// labels for Claude's chat flags (pipeline/chatflags.py, reel.CHAT_CATEGORIES)
const FLAG_LABEL = { slur: 'slur', antisemitism: 'antisemitism', nazi_or_holocaust_comparison: 'Nazi comparison',
  racism_or_bigotry: 'racism / bigotry', dehumanizing: 'dehumanizing', violent_threat_or_glorification: 'violence',
  harassment_or_doxxing: 'harassment', sexual_harassment: 'sexual harassment' };

async function api(path, params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== '' && v !== null && v !== undefined) qs.set(k, v);
  const res = await fetch(path + (qs.toString() ? '?' + qs : ''));
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* not JSON */ }
    throw new Error(res.status === 429 ? 'Too many searches; wait a minute.' : detail);
  }
  return res.json();
}

let CONFIG = null;
async function loadConfig() {
  if (!CONFIG) CONFIG = await api('/api/config');
  return CONFIG;
}

async function chrome(active) {
  const cfg = await loadConfig();
  const link = (href, text) => el('a', { href, text, style: active === href ? 'font-weight:600' : null });
  // every page but the search page itself gets a search box in the header; it opens the search page
  const box = active === 'index.html' ? null : el('form', { class: 'hsearch', action: 'index.html', role: 'search' },
    el('input', { type: 'search', name: 'q', placeholder: 'Search stream and chat…', 'aria-label': 'Search' }),
    el('input', { type: 'hidden', name: 'tab', value: 'transcripts' }));
  document.body.prepend(el('header', {},
    el('a', { href: '/', class: 'brand', text: 'VOD archive' }),
    link('index.html', 'Search'), link('videos.html', 'Videos'), link('reel.html', 'Weekly'), link('factchecks.html', 'Fact-checks'), link('about.html', 'About'), box,
    cfg.mode === 'admin' ? link('flags.html', 'Reports') : null,
    cfg.mode === 'admin' ? el('span', { class: 'badge', text: 'ADMIN · private' }) : null));
  document.body.append(el('footer', { text: 'Transcripts and speaker labels are machine-generated and may contain errors. ' +
    'The linked video is the authoritative record.' }));
  return cfg;
}
