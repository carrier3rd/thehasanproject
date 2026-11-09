// Shared helpers. All DB/user text goes into the DOM through textContent, never innerHTML.
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
  document.body.prepend(el('header', {},
    el('a', { href: 'index.html', class: 'brand', text: 'VOD archive' }),
    link('index.html', 'Search'), link('videos.html', 'Videos'), link('about.html', 'About'),
    cfg.mode === 'admin' ? el('span', { class: 'badge', text: 'ADMIN · private' }) : null));
  document.body.append(el('footer', { text: 'Transcripts and speaker labels are machine-generated and may contain errors. ' +
    'The linked video is the authoritative record.' }));
  return cfg;
}
