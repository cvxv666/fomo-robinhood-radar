"""The case file as a page: the same 1600 x 900 card as make_case.py, replayed.

One self-contained HTML - the case data and the avatars are written into it - that draws the
card in SVG and then plays it: the entry replays as a cursor crossing the timeline, wallets
arriving on the step and in the strip as they arrived on chain, the burst and the alert firing
where they fired; then the candles grow hour by hour, the peak counts up in the corner, and the
clock rail lands. ?still=1 (or reduced motion) shows the finished card; REPLAY plays it again.

    python assets/cases/make_case_html.py catgpt
"""
from __future__ import annotations

import base64
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent

TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;500;700&family=JetBrains+Mono:wght@400;700&display=swap">
<style>
  :root { --black:#000; --white:#fff; --carbon:#606060; --graphite:#949494; --green:#00ff85; --crimson:#ff003d;
          --amber:#ff8a00; --yellow:#fcff76; --violet:#6100ff;
          --sans:"Space Grotesk", system-ui, sans-serif; --mono:"JetBrains Mono", ui-monospace, monospace; }
  html, body { margin:0; height:100%; background:var(--black); color:var(--white); overflow:hidden; }
  .stage { position:fixed; inset:0; display:grid; place-items:center; }
  .board { position:relative; width:1600px; height:900px; transform-origin:center center; background:var(--black); }
  .board > svg { position:absolute; inset:0; width:1600px; height:900px; }
  text { fill:var(--white); font-family:var(--mono); }
  .sans { font-family:var(--sans); }
  .mono { font-family:var(--mono); }
  .b { font-weight:700; }
  .l { font-weight:300; }
  .g { fill:var(--graphite); } .c { fill:var(--carbon); } .gr { fill:var(--green); }
  .replay { cursor:pointer; }
  .replay:hover rect { stroke:var(--green); } .replay:hover text { fill:var(--green); }
</style>
</head>
<body>
<div class="stage"><div class="board" id="board"><svg id="art" viewBox="0 0 1600 900" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"></svg></div></div>
<script>
const CASE = __CASE__;
const AVATARS = __AVATARS__;
(() => {
  const q = new URLSearchParams(location.search);
  const reduced = (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) || q.has('still');
  const board = document.getElementById('board');
  const fit = () => { const s = Math.min(innerWidth / 1600, innerHeight / 900); board.style.transform = `scale(${s})`; };
  addEventListener('resize', fit); fit();

  const NS = 'http://www.w3.org/2000/svg', XL = 'http://www.w3.org/1999/xlink';
  const svg = document.getElementById('art');
  const el = (tag, attrs = {}, parent = svg, textContent = null) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) { if (k === 'href') e.setAttributeNS(XL, 'href', v); else e.setAttribute(k, v); }
    if (textContent !== null) e.textContent = textContent;
    parent.appendChild(e); return e;
  };
  const txt = (x, y, s, attrs = {}, parent = svg) => el('text', { x, y, ...attrs }, parent, s);
  const line = (x1, y1, x2, y2, stroke, attrs = {}, parent = svg) => el('line', { x1, y1, x2, y2, stroke, ...attrs }, parent);
  const W = { WHITE: '#fff', CARBON: '#606060', GRAPHITE: '#949494', GREEN: '#00ff85', CRIMSON: '#ff003d', AMBER: '#ff8a00', YELLOW: '#fcff76', VIOLET: '#6100ff', BLACK: '#000' };
  const pad = 56, w = 1600;
  const hhmm = (ts) => new Date(ts * 1000).toISOString().slice(11, 16);
  const hhmmss = (ts) => new Date(ts * 1000).toISOString().slice(11, 19);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const ease = (p) => 1 - Math.pow(1 - clamp(p, 0, 1), 3);
  const ring = (score) => score >= 80 ? W.GREEN : score >= 70 ? W.AMBER : W.GRAPHITE;

  // ── head
  const g0 = el('g', { transform: `translate(${pad} 22) scale(0.625)` });
  el('rect', { x: .5, y: .5, width: 47, height: 47, stroke: '#fff', fill: 'none' }, g0);
  el('path', { d: 'M42 44 A38 38 0 0 0 4 6', stroke: '#606060', fill: 'none' }, g0);
  el('path', { d: 'M30 44 A26 26 0 0 0 4 18', stroke: '#fcff76', fill: 'none' }, g0);
  el('path', { d: 'M18 44 A14 14 0 0 0 4 30', stroke: '#606060', fill: 'none' }, g0);
  el('path', { d: 'M4 44 L33.1 19.6', stroke: '#fff' }, g0);
  el('circle', { cx: 23.9, cy: 27.3, r: 3, fill: '#00ff85' }, g0);
  const lock = txt(pad + 42, 43, '', { class: 'sans', 'font-size': 19, 'letter-spacing': '.05em' });
  el('tspan', { 'font-weight': 700 }, lock, 'FOMO ');
  el('tspan', { 'font-weight': 300 }, lock, 'ROBINHOOD ');
  el('tspan', { 'font-weight': 700 }, lock, 'RADAR');
  txt(w - pad, 42, `CASE FILE  ·  $${CASE.symbol}  ·  ${CASE.date}  ·  ROBINHOOD CHAIN 4663`, { class: 'g', 'font-size': 11, 'letter-spacing': '.11em', 'text-anchor': 'end' });
  line(pad, 66, w - pad, 66, W.CARBON);

  // ── hero
  const hero = el('g');
  txt(pad - 4, 170, `$${CASE.symbol}`, { class: 'sans l', 'font-size': 84, 'letter-spacing': '.06em' }, hero);
  const pills = CASE.pills.map(([label, colour], i) => {
    const g = el('g', { opacity: 0 }, hero);
    const t = txt(0, 218, label, { fill: colour, 'font-size': 11, 'letter-spacing': '.1em' }, g);
    const r = el('rect', { y: 202, height: 23, rx: 999, fill: 'none', stroke: colour }, g);
    g.insertBefore(r, t);
    return { g, t, r, colour };
  });
  const peakN = txt(w - pad, 172, '', { class: 'sans b gr', 'font-size': 92, 'letter-spacing': '-.02em', 'text-anchor': 'end' }, hero);
  const peakL = txt(w - pad, 200, `PEAK  ·  ${CASE.peak_after} AFTER THE ALERT`, { class: 'g', 'font-size': 11, 'letter-spacing': '.13em', 'text-anchor': 'end', opacity: 0 }, hero);
  const nowL = txt(w - pad, 222, `${CASE.now_multiple.toFixed(1)}× NOW  ·  ${CASE.now_after} LATER`, { 'font-size': 11, 'letter-spacing': '.13em', 'text-anchor': 'end', opacity: 0 }, hero);
  line(pad, 250, w - pad, 250, W.CARBON);

  // ── the entry
  const t0 = CASE.timeline.from, t1 = CASE.timeline.to, span = t1 - t0;
  const tlx0 = pad, tlx1 = w - pad - 70, topY = 334, axisY = 468, cmax = CASE.timeline.conviction_max;
  const X = (ts) => tlx0 + (ts - t0) / span * (tlx1 - tlx0);
  const Y = (c) => axisY - c / cmax * (axisY - topY);
  txt(pad, 276, `THE ENTRY  ·  ${hhmm(t0)} → ${hhmm(t1)} UTC  ·  CONVICTION = Σ (SCORE/100)²  ·  FIRST BUY OF EACH TRUSTED WALLET`, { class: 'g', 'font-size': 11, 'letter-spacing': '.13em' });
  line(tlx0, axisY, tlx1, axisY, W.WHITE);
  for (let tick = t0 - (t0 % 60); tick <= t1; tick += 60) {
    const xx = X(tick); if (xx < tlx0 || xx > tlx1) continue;
    const major = tick % 300 === 0;
    line(xx, axisY, xx, axisY + (major ? 7 : 3), major ? W.WHITE : W.CARBON);
    if (major) txt(xx, axisY + 21, hhmm(tick), { class: 'g', 'font-size': 9.5, 'letter-spacing': '.1em', 'text-anchor': 'middle' });
  }
  const burstBar = {};
  for (const c of [2, 4, 6]) {
    const yy = Y(c), isBar = c === CASE.burst_bar, colour = isBar ? W.YELLOW : W.CARBON;
    const l = line(tlx0, yy, tlx1, yy, colour, { 'stroke-dasharray': '4 4' });
    txt(tlx1 + 8, yy + 3.5, `${c}` + (isBar ? '  BURST BAR' : ''), { fill: colour, 'font-size': 9, 'letter-spacing': '.1em' });
    if (isBar) burstBar.line = l;
  }
  const step = el('path', { d: '', fill: 'none', stroke: W.AMBER, 'stroke-width': 1.6 });
  const entries = CASE.entries.map((e, i) => {
    const conv = CASE.entries.slice(0, i + 1).reduce((s, x) => s + (x.score / 100) ** 2, 0);
    return { ...e, i, x: X(e.ts), y: Y(conv), conv, ring: ring(e.score) };
  });
  const events = CASE.events.map((ev) => {
    const xx = X(ev.ts), ly = topY - 10 - (ev.level || 0) * 15, g = el('g', { opacity: 0 });
    line(xx, ly + 4, xx, axisY, ev.colour, { 'stroke-width': 1.2, ...(ev.dashed ? { 'stroke-dasharray': '4 4' } : {}) }, g);
    const anchor = (ev.anchor || 'l') === 'l' ? 'start' : 'end';
    txt(xx + (anchor === 'start' ? 6 : -6), ly, ev.label + (ev.sub ? `  ·  ${ev.sub}` : ''), { fill: ev.colour, 'font-size': 9.5, 'font-weight': 700, 'letter-spacing': '.08em', 'text-anchor': anchor }, g);
    return { ...ev, g };
  });
  const markers = entries.map((e) => {
    const g = el('g', { opacity: 0 });
    el('circle', { cx: e.x, cy: e.y, r: 6.5, fill: W.BLACK, stroke: e.ring, 'stroke-width': 1.2 }, g);
    txt(e.x, e.y + 2.8, String(e.i + 1), { fill: e.ring, 'font-size': 7.5, 'font-weight': 700, 'text-anchor': 'middle' }, g);
    return g;
  });
  // the cursor: the replay's clock, crossing the timeline
  const cur = el('g', { opacity: 0 });
  line(0, topY - 44, 0, axisY, W.WHITE, { 'stroke-width': 1 }, cur);
  const curT = txt(0, topY - 50, '', { 'font-size': 10, 'font-weight': 700, 'letter-spacing': '.08em', 'text-anchor': 'middle' }, cur);
  const curC = txt(0, topY - 36, '', { class: 'g', 'font-size': 9, 'letter-spacing': '.06em', 'text-anchor': 'middle' }, cur);

  // the cohort, in order of arrival
  const n = entries.length, stripY = 522, cell = (w - 2 * pad) / n, r = 17;
  const defs = el('defs');
  const strip = entries.map((e) => {
    const cx = pad + cell * e.i, ax = cx + r + 1, g = el('g', { opacity: 0 });
    const clip = el('clipPath', { id: `av${e.i}` }, defs); el('circle', { cx: ax, cy: stripY, r }, clip);
    const av = AVATARS[e.handle];
    if (av) el('image', { href: av, x: ax - r, y: stripY - r, width: 2 * r, height: 2 * r, preserveAspectRatio: 'xMidYMid slice', 'clip-path': `url(#av${e.i})` }, g);
    else { el('circle', { cx: ax, cy: stripY, r, fill: '#101010' }, g); txt(ax, stripY + 5.5, e.handle[0].toUpperCase(), { class: 'g', 'font-size': 15, 'font-weight': 700, 'text-anchor': 'middle' }, g); }
    el('circle', { cx: ax, cy: stripY, r, fill: 'none', stroke: e.ring, 'stroke-width': 1.2 }, g);
    el('ellipse', { cx: cx + 6, cy: stripY - r - 1, rx: 7, ry: 7, fill: W.BLACK, stroke: e.ring }, g);
    txt(cx + 6, stripY - r + 1.8, String(e.i + 1), { fill: e.ring, 'font-size': 7.5, 'font-weight': 700, 'text-anchor': 'middle' }, g);
    const tx = cx + 2 * r + 9, handle = e.handle.length <= 11 ? e.handle : e.handle.slice(0, 10) + '…';
    txt(tx, stripY - 5, handle, { fill: e.score >= 80 ? W.WHITE : W.GRAPHITE, 'font-size': 9.5, 'font-weight': 700, 'letter-spacing': '.04em' }, g);
    txt(tx, stripY + 8, `${e.score}  ·  $${e.usd.toLocaleString('en-US')}`, { class: 'g', 'font-size': 8.5, 'letter-spacing': '.04em' }, g);
    txt(tx, stripY + 20, hhmmss(e.ts), { class: 'c', 'font-size': 8.5, 'letter-spacing': '.04em' }, g);
    g.style.transformOrigin = `${ax}px ${stripY}px`;
    return g;
  });
  line(pad, 566, w - pad, 566, W.CARBON);

  // ── what followed
  txt(pad, 592, `WHAT FOLLOWED  ·  HOURLY  ·  × THE ALERT PRICE OF ${CASE.entry_px}`, { class: 'g', 'font-size': 11, 'letter-spacing': '.13em' });
  const cx0 = pad + 30, cx1 = w - pad - 170, cy0 = 612, cy1 = 796, ymax = CASE.candles_max, nc = CASE.candles.length, slot = (cx1 - cx0) / nc;
  const CY = (m) => cy1 - (m / ymax) * (cy1 - cy0);
  line(cx0, cy1, cx1, cy1, W.WHITE);
  for (const m of [1, 5, 10]) { line(cx0, CY(m), cx1, CY(m), W.CARBON, { 'stroke-dasharray': '4 4' }); txt(cx0 - 6, CY(m) + 4, `${m}×`, { class: 'g', 'font-size': 9, 'letter-spacing': '.06em', 'text-anchor': 'end' }); }
  const candles = CASE.candles.map(([ts, o, hi, lo, cl], i) => {
    const cx = cx0 + slot * (i + 0.5), colour = cl >= o ? W.GREEN : W.CRIMSON, g = el('g', { opacity: 0 });
    const wick = line(cx, CY(o), cx, CY(o), colour, { 'stroke-width': 1.2 }, g);
    const body = el('rect', { x: cx - slot * .28, y: CY(o), width: slot * .56, height: 1, fill: cl >= o ? colour : W.BLACK, stroke: colour }, g);
    if (i % 2 === 0) txt(cx, cy1 + 20, hhmm(ts), { class: 'g', 'font-size': 9.5, 'letter-spacing': '.1em', 'text-anchor': 'middle' });
    return { g, wick, body, o, hi, lo, cl, cx };
  });
  const callouts = CASE.callouts.map((co) => {
    const cx = cx0 + slot * (co.index + .5), yy = CY(co.multiple), dx = co.dx ?? 14, dy = co.dy ?? -14, g = el('g', { opacity: 0 });
    el('circle', { cx, cy: yy, r: 4, fill: 'none', stroke: co.colour, 'stroke-width': 1.2 }, g);
    line(cx, yy, cx + dx, yy + dy, co.colour, {}, g);
    const anchor = dx < 0 ? 'end' : 'start', lx = cx + dx + (dx > 0 ? 4 : -4);
    txt(lx, yy + dy + 2, co.label, { fill: co.colour, 'font-size': 10, 'font-weight': 700, 'letter-spacing': '.08em', 'text-anchor': anchor }, g);
    if (co.sub) txt(lx, yy + dy + 15, co.sub, { class: 'g', 'font-size': 9, 'letter-spacing': '.06em', 'text-anchor': anchor }, g);
    return { ...co, g };
  });
  const rx = cx1 + 34;
  const clock = CASE.clock.map(([k, v, colour], j) => {
    const yy = 640 + j * 58, g = el('g', { opacity: 0 });
    txt(rx, yy, k, { class: 'g', 'font-size': 9.5, 'letter-spacing': '.12em' }, g);
    txt(rx, yy + 26, v, { class: 'sans b', fill: colour, 'font-size': 24, 'letter-spacing': '.02em' }, g);
    return g;
  });

  // ── foot, and the replay control
  line(pad, 838, w - pad, 838, W.CARBON);
  const foot = el('g', { opacity: 0 });
  txt(pad, 868, CASE.foot_left, { 'font-size': 11, 'letter-spacing': '.13em' }, foot);
  txt(w - pad, 868, CASE.foot_right, { class: 'g', 'font-size': 11, 'letter-spacing': '.13em', 'text-anchor': 'end' }, foot);
  const rp = el('g', { class: 'replay', opacity: 0 });
  el('rect', { x: w - pad - 84, y: 264, width: 84, height: 20, rx: 999, fill: 'none', stroke: W.CARBON }, rp);
  txt(w - pad - 42, 278, '↻  REPLAY', { class: 'g', 'font-size': 9.5, 'letter-spacing': '.12em', 'text-anchor': 'middle' }, rp);

  // pills need their text measured, which needs the fonts
  const layoutPills = () => { let x = pad; for (const p of pills) { const tw = p.t.getComputedTextLength(); p.r.setAttribute('x', x); p.r.setAttribute('width', tw + 25.3); p.t.setAttribute('x', x + 12.65); x += tw + 25.3 + 10; } };

  // ── the replay: everything is a function of one clock, so a still is just the end of it
  const T = { pills: 0.2, entry: 1.2, entryDur: 8.0, candles: 9.6, per: 0.3, clock: 13.4, end: 14.6 };
  const burstEv = CASE.events.find((e) => e.label.startsWith('BURST')), alertEv = CASE.events.find((e) => e.label.startsWith('ALERT'));
  const peakIdx = CASE.callouts.find((c) => c.label.startsWith('PEAK'))?.index ?? 0;
  const render = (t) => {
    // hero pills, one after another
    pills.forEach((p, i) => p.g.setAttribute('opacity', ease((t - T.pills - i * .15) / .3)));
    // the entry: the cursor's time
    const tau = t0 + clamp((t - T.entry) / T.entryDur, 0, 1) * span;
    const running = t >= T.entry && t < T.entry + T.entryDur;
    cur.setAttribute('opacity', running ? 1 : 0);
    if (running) { const xx = X(tau); cur.setAttribute('transform', `translate(${xx} 0)`); curT.textContent = hhmmss(Math.floor(tau)); const c = entries.filter((e) => e.ts <= tau).length; curC.textContent = c ? `Σ ${entries[c - 1].conv.toFixed(2)}  ·  ${c} IN` : 'WAITING'; }
    // the step, up to tau
    let d = `M${tlx0} ${Y(0)}`, py = Y(0), px = tlx0;
    for (const e of entries) { if (e.ts > tau) break; d += ` H${e.x} V${e.y}`; px = e.x; py = e.y; }
    d += ` H${t >= T.entry ? Math.min(tlx1, X(tau)) : tlx0}`;
    step.setAttribute('d', d);
    entries.forEach((e, i) => { const on = e.ts <= tau ? 1 : 0; markers[i].setAttribute('opacity', on); strip[i].setAttribute('opacity', on); const k = e.ts <= tau ? ease((tau - e.ts) / 40 + (t >= T.entry + T.entryDur ? 1 : 0)) : 0; strip[i].style.transform = `scale(${0.6 + 0.4 * k})`; });
    events.forEach((ev) => ev.g.setAttribute('opacity', ev.ts <= tau ? 1 : 0));
    // the burst bar flares when the bar is crossed; the alert pill buzzes when the alert goes
    if (burstBar.line && burstEv) { const f = clamp(1 - (tau - burstEv.ts) / 60, 0, 1) * (tau >= burstEv.ts ? 1 : 0); burstBar.line.setAttribute('stroke-width', 1 + f * 2); }
    if (alertEv) { const s = (tau - alertEv.ts) / 40; const buzz = tau >= alertEv.ts && s < 3 ? (Math.floor(s * 2) % 2 === 0 ? 1 : 0.35) : 1; const p = pills[pills.length - 1]; p.r.setAttribute('fill', tau >= alertEv.ts && s < 3 && buzz === 1 ? p.colour : 'none'); p.t.setAttribute('fill', tau >= alertEv.ts && s < 3 && buzz === 1 ? W.BLACK : p.colour); }
    // what followed: candles grow hour by hour; the peak counts up in the corner as its candle does
    candles.forEach((c, i) => { const p = ease((t - T.candles - i * T.per) / .45); c.g.setAttribute('opacity', p > 0 ? 1 : 0);
      const top = c.o + (Math.max(c.o, c.cl) - c.o) * p, bot = c.o - (c.o - Math.min(c.o, c.cl)) * p;
      c.body.setAttribute('y', CY(top)); c.body.setAttribute('height', Math.max(1, CY(bot) - CY(top)));
      c.wick.setAttribute('y1', CY(c.o + (c.hi - c.o) * p)); c.wick.setAttribute('y2', CY(c.o - (c.o - c.lo) * p)); });
    callouts.forEach((co) => co.g.setAttribute('opacity', ease((t - T.candles - co.index * T.per - .45) / .3)));
    const pk = t < T.candles ? 0 : ease((t - T.candles - peakIdx * T.per) / .6);
    peakN.textContent = t < T.pills ? '' : `${(1 + (CASE.peak_multiple - 1) * pk).toFixed(1)}×`;
    peakL.setAttribute('opacity', ease((t - T.candles - peakIdx * T.per - .4) / .3));
    nowL.setAttribute('opacity', ease((t - T.candles - (nc - 1) * T.per - .3) / .3));
    clock.forEach((g, j) => g.setAttribute('opacity', ease((t - T.clock - j * .25) / .3)));
    foot.setAttribute('opacity', ease((t - T.clock - .8) / .4));
    rp.setAttribute('opacity', t >= T.end ? 1 : 0);
  };
  let start = performance.now(), raf = 0;
  const play = () => { cancelAnimationFrame(raf); start = performance.now(); const loop = () => { const t = (performance.now() - start) / 1000; render(Math.min(t, T.end)); if (t < T.end) raf = requestAnimationFrame(loop); }; raf = requestAnimationFrame(loop); };
  rp.addEventListener('click', play);
  (document.fonts ? document.fonts.ready : Promise.resolve()).then(() => { layoutPills(); if (reduced) render(T.end); else play(); });
})();
</script>
</body>
</html>
'''


def build(slug: str) -> pathlib.Path:
    folder = HERE / slug
    case = json.loads((folder / "case.json").read_text(encoding="utf-8"))
    avatars = {}
    for e in case["entries"]:
        for ext, mime in (("jpg", "image/jpeg"), ("png", "image/png")):
            p = folder / "avatars" / f"{e['handle']}.{ext}"
            if p.exists():
                avatars[e["handle"]] = f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")
                break
    html = (TEMPLATE.replace("__TITLE__", f"${case['symbol']} — case file")
            .replace("__CASE__", json.dumps(case, ensure_ascii=False))
            .replace("__AVATARS__", json.dumps(avatars)))
    out = folder / f"{slug}.html"
    out.open("w", encoding="utf-8", newline="").write(html)
    print(f"{out.relative_to(HERE.parent.parent)}  {out.stat().st_size // 1024} KB, {len(avatars)} avatars embedded")
    return out


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "catgpt")
