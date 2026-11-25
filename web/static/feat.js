// Shared bits for the featured pages (Spotlight, Receipts, Chat vs Hasan, Quiz). Needs common.js first.

const KIND_LABEL = { news_politics: 'Politics', israel_palestine: 'Israel–Palestine', war_and_foreign_policy: 'War & foreign policy',
  streamers_and_internet: 'Streamers & internet', culture_and_entertainment: 'Culture', games: 'Games', sports: 'Sports',
  science_tech_health: 'Science & tech', personal_or_stream: 'His life & the stream', other: 'Other' };
const isoDay = d => d.toISOString().slice(0, 10);
const niceDay = s => { const d = new Date(s + 'T12:00:00'); return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }); };
const shortDay = s => { const d = new Date(s + 'T12:00:00'); return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); };
const mins = m => m >= 90 ? `${(m / 60).toFixed(1)} h` : `${Math.round(m)} min`;
const watchAt = (vid, t) => watchUrl(vid, Math.max(0, (t || 0) - 5));

async function feature(name) {
  try { return await api('/api/features/' + name); } catch (e) { return null; }
}

function daysBetween(a, b) {
  const out = [];
  for (let d = new Date(a + 'T12:00:00'); isoDay(d) <= b; d.setDate(d.getDate() + 1)) out.push(isoDay(d));
  return out;
}

// One single-series bar chart per measure (never two scales on one chart). days: ['YYYY-MM-DD'...];
// value(day) -> number or null (null = no data that day, drawn as a hatched gap, never as zero).
// A tooltip follows the pointer; a table version sits under it for screen readers and keyboard users.
let chartN = 0;
function barChart({ title, days, value, color, unit, emptyNote }) {
  const id = 'c' + (++chartN), W = 640, H = 120, pad = { l: 34, r: 6, t: 8, b: 20 };
  const vals = days.map(value), max = Math.max(1, ...vals.filter(v => v != null));
  const bw = (W - pad.l - pad.r) / Math.max(1, days.length), y = v => pad.t + (H - pad.t - pad.b) * (1 - v / max);
  const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
  const mk = (tag, a) => { const n = document.createElementNS(NS, tag); for (const k in a) n.setAttribute(k, a[k]); return n; };
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('class', 'fchart'); svg.setAttribute('role', 'img');
  svg.setAttribute('aria-labelledby', id + 't');
  const defs = mk('defs', {}), pat = mk('pattern', { id: id + 'h', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
  pat.append(mk('rect', { width: 6, height: 6, class: 'gapbg' }), mk('line', { x1: 0, y1: 0, x2: 0, y2: 6, class: 'gapline' }));
  defs.append(pat); svg.append(defs);
  for (const f of [0.5, 1]) {
    svg.append(mk('line', { x1: pad.l, x2: W - pad.r, y1: y(max * f), y2: y(max * f), class: 'grid' }));
    const t = mk('text', { x: pad.l - 4, y: y(max * f) + 4, class: 'axis', 'text-anchor': 'end' });
    t.textContent = max * f >= 10 ? Math.round(max * f) : +(max * f).toFixed(1);
    svg.append(t);
  }
  svg.append(mk('line', { x1: pad.l, x2: W - pad.r, y1: H - pad.b, y2: H - pad.b, class: 'base' }));
  days.forEach((d, i) => {
    const v = vals[i], x = pad.l + i * bw;
    if (v == null) svg.append(mk('rect', { x, y: pad.t, width: Math.max(1, bw), height: H - pad.t - pad.b, fill: `url(#${id}h)` }));
    else if (v > 0) {
      const h = Math.max(2, H - pad.b - y(v));
      svg.append(mk('rect', { x: x + Math.min(1, bw * 0.15), y: H - pad.b - h, width: Math.max(1, bw - Math.min(2, bw * 0.3)), height: h,
        rx: Math.min(2, bw / 3), fill: color }));
    }
  });
  const ticks = Math.min(6, days.length);
  for (let k = 0; k < ticks; k++) {
    const i = Math.round(k * (days.length - 1) / Math.max(1, ticks - 1));
    const t = mk('text', { x: pad.l + i * bw + bw / 2, y: H - 5, class: 'axis', 'text-anchor': k === 0 ? 'start' : k === ticks - 1 ? 'end' : 'middle' });
    t.textContent = shortDay(days[i]); svg.append(t);
  }
  const cursor = mk('rect', { x: 0, y: pad.t, width: bw, height: H - pad.t - pad.b, class: 'cursor', visibility: 'hidden' });
  svg.append(cursor);
  const tip = el('div', { class: 'ftip', hidden: '' });
  const wrap = el('figure', { class: 'fig' }, el('figcaption', { id: id + 't', text: title }), el('div', { class: 'fplot' }, svg, tip));
  const show = e => {
    const r = svg.getBoundingClientRect(), sx = (e.clientX - r.left) * W / r.width;
    const i = Math.floor((sx - pad.l) / bw);
    if (i < 0 || i >= days.length) { tip.hidden = true; cursor.setAttribute('visibility', 'hidden'); return; }
    cursor.setAttribute('x', pad.l + i * bw); cursor.setAttribute('visibility', 'visible');
    tip.textContent = `${niceDay(days[i])}: ${vals[i] == null ? (emptyNote || 'no data') : `${+vals[i].toFixed(1)} ${unit}`}`;
    tip.hidden = false;
    const left = Math.min(Math.max(0, e.clientX - r.left - 60), r.width - 150);
    tip.style.left = left + 'px';
  };
  svg.addEventListener('pointermove', show);
  svg.addEventListener('pointerdown', show);
  svg.addEventListener('pointerleave', () => { tip.hidden = true; cursor.setAttribute('visibility', 'hidden'); });
  const rows = days.map((d, i) => [d, vals[i]]).filter(([, v]) => v == null || v > 0);
  wrap.append(el('details', { class: 'ftable' }, el('summary', { text: 'Show as a table' }),
    el('table', {}, el('thead', {}, el('tr', {}, el('th', { text: 'Day' }), el('th', { text: unit }))),
      el('tbody', {}, ...rows.map(([d, v]) => el('tr', {}, el('td', { text: niceDay(d) }), el('td', { class: 'num', text: v == null ? (emptyNote || 'no data') : +v.toFixed(1) })))))));
  return wrap;
}

// Horizontal bars, one row per community; the number is always written out next to the bar.
function hbars(rows, unit) {
  const max = Math.max(0.0001, ...rows.map(r => r.value));
  return el('div', { class: 'hbars' }, ...rows.map(r => el('div', { class: 'hbar' },
    r.href ? el('a', { class: 'hl', href: r.href, text: r.label }) : el('span', { class: 'hl', text: r.label }),
    el('span', { class: 'ht' }, el('span', { class: 'hf', style: `width:${Math.max(r.value > 0 ? 2 : 0, 100 * r.value / max)}%` })),
    el('span', { class: 'hv num', text: r.text ?? `${r.value} ${unit}` }))));
}

function copyLink(url, btn) {
  const done = () => { const t = btn.textContent; btn.textContent = 'Link copied'; setTimeout(() => { btn.textContent = t; }, 1600); };
  if (navigator.share && /Android|iPhone|iPad/i.test(navigator.userAgent)) navigator.share({ url }).catch(() => {});
  else if (navigator.clipboard) navigator.clipboard.writeText(url).then(done, () => prompt('Copy this link', url));
  else prompt('Copy this link', url);
}

function chips(container, options, current, onPick, label) {
  container.setAttribute('role', 'group');
  if (label) container.setAttribute('aria-label', label);
  container.replaceChildren(...options.map(([value, text]) => {
    const b = el('button', { type: 'button', class: 'chip' + (value === current ? ' on' : ''), text,
      'aria-pressed': value === current ? 'true' : 'false' });
    b.addEventListener('click', () => onPick(value));
    return b;
  }));
}
