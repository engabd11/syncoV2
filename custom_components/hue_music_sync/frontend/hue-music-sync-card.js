/**
 * Hue Music Sync Card - "Ambient Glow"
 * A Home Assistant custom Lovelace card for the Hue Music Sync integration.
 *
 * Faithful re-implementation of the "Ambient Glow" design (Variation B):
 * immersive colour-bleed hero, blurred album-art backdrop, ambient visualizer,
 * Hue dark-navy theme.
 *
 * Bundled with and served by the integration (no separate install). Single
 * self-contained custom element - no build step. See README.md for config.
 */

// Cosmetic version (shown in the console banner). The browser cache-bust no
// longer depends on this: the integration appends ?v=<content-hash> derived from
// this file's bytes, so any edit is picked up without a manual hard refresh.
const VERSION = "1.22.0";

/* ------------------------- Palette data ------------------------- */
// Colour schemes from the integration, each a small gradient swatch.
// `match` is the normalised key used to map a colour-select option to a swatch.
const PALETTES = [
  { id: "album",    name: "Album colours", colors: ["#ff2d7e", "#7b5cff", "#27d3ff"], album: true,
    aliases: ["album", "albumcolours", "albumcolors", "albumart"] },
  { id: "rainbow",  name: "Rainbow",       colors: ["#ff3b3b", "#ffd23b", "#3bff7a", "#3bc9ff", "#b03bff"] },
  { id: "sunset",   name: "Sunset",        colors: ["#ff7a3d", "#ff4d8d", "#9b4dff"] },
  { id: "ocean",    name: "Ocean",         colors: ["#1fd7c1", "#1f9bff", "#2c4bff"] },
  { id: "forest",   name: "Forest",        colors: ["#9ad62e", "#27c46b", "#0f8f6b"] },
  { id: "lavender", name: "Lavender",      colors: ["#c79bff", "#9b6cff", "#6f5cff"] },
  { id: "ember",    name: "Ember",         colors: ["#ffb13d", "#ff5a2e", "#c41f4b"] },
  { id: "aurora",   name: "Aurora",        colors: ["#2effb0", "#27d3ff", "#9b6cff"] },
  { id: "tropical", name: "Tropical",      colors: ["#19e3c4", "#9ff52e", "#ff7a5a"], scene: true },
  { id: "savanna",  name: "Savanna",       colors: ["#ffcf57", "#ff9d3d", "#d9692e"], scene: true },
  { id: "blossom",  name: "Blossom",       colors: ["#ffb3d9", "#ff6fb0", "#c44d9b"], scene: true },
  { id: "honolulu", name: "Honolulu",      colors: ["#ff9a3d", "#ff4d7a", "#a34dff"], scene: true },
  { id: "galaxy",   name: "Galaxy",        colors: ["#3d6bff", "#7b3dff", "#ff3dd0"], scene: true },
];

const DEFAULT_INTENSITIES = ["Auto", "Subtle", "Medium", "High", "Intense"];
const DEFAULT_EFFECTS = ["Movie", "Music", "Fireworks"];
const DEFAULT_SWATCH = ["#6f6c86", "#4a4862"];

const DEMO_AREAS = [
  { name: "Living Room" },
  { name: "Bedroom" },
  { name: "Office" },
  { name: "Kitchen" },
];

const DEMO_NOW = { track: "Neon Tide", artist: "Solenne", art: null, duration: 247 };

/* ------------------------- Helpers ------------------------- */
const normalise = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

const titleize = (s) =>
  String(s || "")
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());

function gradFor(colors, angle = 135) {
  if (!colors || colors.length === 0) return DEFAULT_SWATCH[0];
  if (colors.length === 1) return colors[0];
  return `linear-gradient(${angle}deg, ${colors.join(", ")})`;
}

function matchPalette(option) {
  const key = normalise(option);
  return (
    PALETTES.find((p) => (p.aliases || []).includes(key)) ||
    PALETTES.find((p) => normalise(p.id) === key || normalise(p.name) === key) ||
    null
  );
}

/* -- colour utilities (album-art extraction + parsing integration colours) -- */
function rgbToHex(r, g, b) {
  const h = (v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0");
  return "#" + h(r) + h(g) + h(b);
}

function rgbToHsv(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let h = 0;
  if (d) {
    if (mx === r) h = ((g - b) / d) % 6;
    else if (mx === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60; if (h < 0) h += 360;
  }
  return { h, s: mx ? d / mx : 0, v: mx };
}

function hsvToRgb(h, s, v) {
  const c = v * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = v - c;
  let r = 0, g = 0, b = 0;
  if (h < 60) [r, g, b] = [c, x, 0];
  else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x];
  else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}

// Lift an extracted colour to a vivid, light-friendly version (album covers are
// often dark/muted; the integration drives the lights with a vivid palette, so
// the card should match rather than render a muddy swatch).
function vivify(r, g, b) {
  let { h, s, v } = rgbToHsv(r, g, b);
  s = Math.min(1, Math.max(0.6, s * 1.2));
  v = Math.min(1, Math.max(0.78, v));
  return rgbToHex(...hsvToRgb(h, s, v));
}

// Frequency-spectrum colour for the room map: t=0 is the low end (bass, warm
// red) and t=1 the high end (treble, cool violet), passing through the whole
// spectrum. Each lamp reacts to its own slice, so its dot is tinted by where it
// sits in the spectrum (which is its left-to-right position in the room).
function spectrumColor(t) {
  t = Math.max(0, Math.min(1, t));
  return rgbToHex(...hsvToRgb(8 + t * 272, 0.78, 1));
}

// Accept colours the integration may publish: ["#rrggbb", ...], ["r,g,b", ...],
// [[r,g,b], ...] (0-255 or 0-1 floats). Returns ["#rrggbb", ...] or null.
function parseColorList(val) {
  if (!val) return null;
  let arr = val;
  if (typeof val === "string") {
    try { arr = JSON.parse(val); } catch (_) { arr = val.split(/[;|]/); }
  }
  if (!Array.isArray(arr) || !arr.length) return null;
  const out = [];
  for (let c of arr) {
    if (Array.isArray(c) && c.length >= 3) {
      let [r, g, b] = c;
      if (r <= 1 && g <= 1 && b <= 1) { r *= 255; g *= 255; b *= 255; }
      out.push(rgbToHex(r, g, b));
      continue;
    }
    if (typeof c === "number") continue;
    c = String(c).trim();
    if (/^#?[0-9a-fA-F]{6}$/.test(c)) { out.push(c[0] === "#" ? c : "#" + c); continue; }
    const m = c.match(/(\d+)\D+(\d+)\D+(\d+)/);
    if (m) out.push(rgbToHex(+m[1], +m[2], +m[3]));
  }
  return out.length ? out : null;
}

// Pull a small vibrant palette out of downscaled album-art pixel data.
function extractVibrant(data, k) {
  const buckets = new Map();
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 125) continue; // skip transparent
    const r = data[i], g = data[i + 1], b = data[i + 2];
    const key = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4);
    let e = buckets.get(key);
    if (!e) { e = { n: 0, r: 0, g: 0, b: 0 }; buckets.set(key, e); }
    e.n++; e.r += r; e.g += g; e.b += b;
  }
  if (!buckets.size) return null;
  const cands = [];
  for (const e of buckets.values()) {
    const r = e.r / e.n, g = e.g / e.n, b = e.b / e.n;
    const { h, s, v } = rgbToHsv(r, g, b);
    // vibrancy dominates; dampen sheer population (sqrt) so a dark background
    // can't outvote a smaller vivid region. Near-black/grey score ~0.
    const vibrancy = s * (0.45 + 0.55 * (1 - Math.abs(v - 0.62)));
    cands.push({ r, g, b, h, s, v, score: (0.06 + vibrancy) * Math.sqrt(e.n) });
  }
  cands.sort((a, b) => b.score - a.score);
  // Primary pass: distinct, reasonably vivid hues.
  const picked = [];
  const distinct = (c, minHue) =>
    picked.every((p) => Math.min(Math.abs(p.h - c.h), 360 - Math.abs(p.h - c.h)) > minHue || Math.abs(p.v - c.v) > 0.3);
  for (const c of cands) {
    if (picked.length >= k) break;
    if (c.v < 0.18 || c.s < 0.22) continue; // skip near-black and washed-out greys
    if (distinct(c, 28)) picked.push(c);
  }
  // Backfill from the best remaining buckets if the art is low on vivid colour.
  for (const c of cands) {
    if (picked.length >= k) break;
    if (c.v < 0.12) continue;
    if (!picked.includes(c) && distinct(c, 12)) picked.push(c);
  }
  const hexes = picked.slice(0, k).map((c) => vivify(c.r, c.g, c.b));
  while (hexes.length && hexes.length < k) hexes.push(hexes[hexes.length - 1]);
  return hexes.length ? hexes : null;
}

/* ------------------------- Styles (ported from the design) ------------------------- */
const CARD_CSS = `
  :host {
    --hue-bg: #0b0a14;
    --hue-card: #161526;
    --hue-line: rgba(255,255,255,0.075);
    --hue-text: #f1eef9;
    --hue-dim: #a7a4be;
    --hue-faint: #6f6c86;
    --hk: "Hanken Grotesk", var(--paper-font-common-base_-_font-family, system-ui), system-ui, sans-serif;
    display: block;
  }
  * { box-sizing: border-box; }

  /* -- card shell -- */
  .hue-card {
    position: relative;
    width: 100%;
    background: linear-gradient(180deg, var(--hue-card) 0%, #121120 100%);
    border: 1px solid var(--hue-line);
    border-radius: 26px;
    color: var(--hue-text);
    overflow: hidden;
    isolation: isolate;
    font-family: var(--hk);
    transition: box-shadow .4s;
  }

  /* -- now playing text -- */
  .hue-now-meta { flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: center; }
  .hue-now-track { font-weight: 700; font-size: 16px; letter-spacing: -.01em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hue-now-artist { font-size: 12.5px; color: var(--hue-dim); margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* -- album cover -- */
  .hue-cover { position: relative; flex: none; overflow: hidden; box-shadow: 0 8px 22px -6px #000a, inset 0 0 0 1px #fff1; }
  .hue-cover-art {
    position: absolute; inset: 0; background-size: cover; background-position: center;
    background-image:
      radial-gradient(60% 70% at 25% 25%, #ff2d7e 0%, transparent 55%),
      radial-gradient(70% 80% at 80% 30%, #7b5cff 0%, transparent 55%),
      radial-gradient(80% 90% at 60% 90%, #27d3ff 0%, transparent 60%),
      linear-gradient(140deg, #1a0f2e, #0e1430);
    filter: saturate(1.15);
  }
  .hue-cover-gloss { position: absolute; inset: 0; background: linear-gradient(160deg, #ffffff30, transparent 40%); mix-blend-mode: screen; transition: opacity .15s; }

  /* -- area selector (themed dropdown) -- */
  .hue-area-dd { position: relative; display: inline-block; margin-bottom: 16px; max-width: 100%; }
  .hue-area-trigger { display: inline-flex; align-items: center; gap: 8px; max-width: 100%;
    padding: 8px 13px; border-radius: 12px; background: #ffffff0a; border: 1px solid var(--hue-line);
    color: var(--hue-text); font-family: var(--hk); font-size: 13px; font-weight: 700;
    cursor: pointer; transition: .18s; white-space: nowrap; }
  .hue-area-trigger:hover { background: #ffffff14; }
  .hue-area-trigger.static { cursor: default; }
  .hue-area-dot { width: 8px; height: 8px; border-radius: 50%; transition: .18s; flex: none; }
  .hue-area-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hue-area-badge { font-size: 10px; font-weight: 800; letter-spacing: .05em; text-transform: uppercase; flex: none; }
  .hue-area-caret { font-size: 8px; color: var(--hue-dim); margin-left: 1px; flex: none; transition: transform .18s; }
  .hue-area-dd.open .hue-area-caret { transform: rotate(180deg); }
  /* Fixed-position popover: escapes the card's overflow:hidden (which used to
     clip long area lists at the card edge) and scrolls internally when the list
     is taller than the space available. Anchored to the trigger via JS. */
  .hue-area-menu { position: fixed; z-index: 60; visibility: hidden; min-width: 210px; max-width: 280px;
    background: #17162a; border: 1px solid var(--hue-line); border-radius: 14px; padding: 6px;
    box-shadow: 0 18px 44px -14px #000d, 0 0 0 1px #ffffff08; display: flex; flex-direction: column; gap: 2px;
    backdrop-filter: blur(12px); overflow-y: auto; overscroll-behavior: contain;
    scrollbar-width: thin; scrollbar-color: #ffffff2e transparent; }
  .hue-area-menu::-webkit-scrollbar { width: 6px; }
  .hue-area-menu::-webkit-scrollbar-thumb { background: #ffffff2e; border-radius: 3px; }
  .hue-area-row { flex: none; }
  .hue-area-row { display: flex; align-items: center; gap: 9px; padding: 9px 10px; border-radius: 10px;
    background: none; border: none; text-align: left; width: 100%; cursor: pointer; color: var(--hue-dim);
    font-family: var(--hk); font-size: 13px; font-weight: 600; transition: .14s; }
  .hue-area-row:hover { background: #ffffff12; color: var(--hue-text); }
  .hue-area-row.sel { background: #ffffff10; color: var(--hue-text); }
  .hue-area-row-name { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hue-area-check { width: 13px; text-align: center; font-size: 12px; font-weight: 800; flex: none; }
  /* two-line menu rows (player menu: name + now-playing/state sub line) */
  .hue-area-row-col { display: flex; flex-direction: column; min-width: 0; flex: 1; text-align: left; }
  .hue-area-row-col .hue-area-row-name { flex: none; }
  .hue-area-row-sub { font-size: 10.5px; font-weight: 600; color: var(--hue-faint);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  /* area/player dropdowns side by side under their section titles */
  .hue-selects { margin-bottom: 14px; }
  .hue-field .hue-area-dd { margin-bottom: 0; display: block; }
  .hue-field .hue-area-trigger { width: 100%; justify-content: flex-start; }
  .hue-field .hue-area-trigger .hue-area-name { flex: 1; text-align: left; min-width: 0; }

  /* -- fields / labels -- */
  .hue-field { display: flex; flex-direction: column; gap: 8px; margin-bottom: 14px; }
  .hue-field:last-child { margin-bottom: 0; }
  .hue-label { font-size: 11px; font-weight: 700; letter-spacing: .07em; text-transform: uppercase; color: var(--hue-faint);
    display: flex; align-items: center; justify-content: space-between; }
  .hue-label-val { color: var(--hue-dim); font-weight: 600; letter-spacing: 0; text-transform: none; font-size: 12px; }
  .hue-grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 16px; }
  .hue-grid2.tight { gap: 12px 16px; }
  .hue-grid2 .hue-field { margin-bottom: 0; }

  /* -- segmented -- */
  .hue-seg { display: flex; gap: 4px; padding: 4px; background: #00000033; border: 1px solid var(--hue-line); border-radius: 12px; }
  .hue-seg-btn { position: relative; flex: 1; padding: 7px 4px; border: none; background: transparent; border-radius: 9px;
    color: var(--hue-dim); font-family: var(--hk); font-size: 11.5px; font-weight: 600; cursor: pointer; transition: .16s; overflow: hidden; min-width: 0; }
  .hue-seg-btn:hover { color: var(--hue-text); }
  .hue-seg-btn.on { color: #fff; background: #ffffff0e; }
  .hue-seg-label { position: relative; z-index: 1; white-space: nowrap; }
  .hue-seg-glow { position: absolute; inset: 0; opacity: .14; }

  /* -- Auto enabled-rungs checklist (shown when Intensity = Auto). Reuses the
     area-dropdown trigger/menu/row styling; only spacing differs. -- */
  .hue-autorange { margin-top: 8px; }
  .hue-autorange .hue-area-trigger { width: 100%; }

  /* -- palette dots -- */
  .hue-dots { display: flex; align-items: center; flex-wrap: wrap; gap: 9px; }
  .hue-dot { position: relative; border: none; border-radius: 50%; cursor: pointer; padding: 0; width: 26px; height: 26px;
    transition: transform .16s, box-shadow .2s; outline: 1px solid #ffffff1f; outline-offset: -1px; }
  .hue-dot:hover { transform: scale(1.12); }
  .hue-dot.on { transform: scale(1.06); }
  .hue-dot-ring { position: absolute; inset: 3px; border-radius: 50%; border: 1.5px dashed #ffffffcc; opacity: .8; }

  /* -- slider -- */
  .hue-slider-row { display: flex; align-items: center; gap: 11px; }
  .hue-slider-icon { font-size: 14px; color: var(--hue-dim); width: 16px; text-align: center; }
  .hue-slider { position: relative; flex: 1; height: 22px; display: flex; align-items: center; cursor: pointer; touch-action: none; }
  .hue-slider-track { position: absolute; left: 0; right: 0; height: 6px; border-radius: 6px; background: #ffffff14; }
  .hue-slider-fill { position: absolute; left: 0; height: 6px; border-radius: 6px; }
  .hue-slider-knob { position: absolute; width: 16px; height: 16px; border-radius: 50%; transform: translateX(-50%); border: 2px solid #fff; }
  .hue-slider-val { font-size: 13px; font-weight: 700; min-width: 38px; text-align: right; font-variant-numeric: tabular-nums; }
  .hue-slider-suf { font-size: 10px; color: var(--hue-faint); margin-left: 1px; font-weight: 600; }

  /* -- timing offset (precise stepper) -- */
  .hue-timing { display: flex; align-items: center; gap: 8px; }
  .hue-step { width: 34px; height: 34px; flex: none; border-radius: 10px; border: 1px solid var(--hue-line); background: #ffffff0a;
    color: var(--hue-text); font-size: 19px; line-height: 1; cursor: pointer; font-family: var(--hk); transition: .15s;
    display: flex; align-items: center; justify-content: center; }
  .hue-step:hover { background: #ffffff18; }
  .hue-step:active { transform: scale(.92); }
  .hue-step:disabled { cursor: default; }
  .hue-step:disabled:hover { background: #ffffff0a; }
  /* Auto-timing toggle: a compact "A" that lights up when calibration is on. */
  .hue-timing-auto { font-size: 14px; font-weight: 800; }
  .hue-timing-auto.on { border-color: transparent; }
  .hue-timing-readout { flex: 1; height: 34px; border-radius: 10px; background: #00000033;
    display: flex; align-items: baseline; justify-content: center; gap: 3px; }
  .hue-timing-num { font-size: 16px; font-weight: 800; font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
  .hue-timing-unit { font-size: 11px; font-weight: 700; color: var(--hue-faint); }

  /* -- advanced live tunables -- */
  .hue-advanced { display: flex; flex-direction: column; }
  .hue-adv-toggle { align-self: flex-start; display: inline-flex; align-items: center; gap: 6px;
    font-size: 11px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase;
    color: var(--hue-dim); background: #ffffff10; border: none; border-radius: 9px;
    padding: 6px 12px; cursor: pointer; transition: .18s; }
  .hue-adv-toggle.on { color: #fff; }
  .hue-tun-knobs { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
  .hue-tun-row { gap: 9px; }
  .hue-tun-label { font-size: 12px; color: var(--hue-dim); min-width: 108px;
    display: flex; align-items: center; gap: 6px; }
  .hue-adv-reset { align-self: flex-start; margin-top: 10px; font-size: 11px;
    color: var(--hue-faint); background: none; border: none; cursor: pointer; }

  /* -- power switch -- */
  .hue-power { position: relative; width: 52px; height: 30px; border-radius: 999px; border: 1px solid var(--hue-line);
    background: #ffffff12; cursor: pointer; transition: .22s; padding: 0; flex: none; }
  .hue-power-knob { position: absolute; top: 3px; left: 3px; width: 24px; height: 24px; border-radius: 50%; background: #cfcce0;
    transition: .22s cubic-bezier(.3,1.4,.5,1); }
  .hue-power.on .hue-power-knob { left: calc(100% - 27px); background: #fff; }

  /* -- bars visualizer -- */
  .hue-bars { display: flex; align-items: flex-end; width: 100%; height: 64px; gap: 3px; }
  .hue-bar { flex: 1; min-width: 0; border-radius: 4px; }

  /* -- ambient hero -- */
  .hue-hero { position: relative; padding: 18px 20px 16px; overflow: hidden; }
  /* Blurred album art behind the colour wash: the card becomes "this song's
     card". Hidden (opacity 0) until the art URL has actually loaded. */
  .hue-hero-art { position: absolute; inset: -24%; z-index: 0; background-size: cover;
    background-position: center; filter: blur(26px) saturate(1.25) brightness(0.62);
    opacity: 0; transition: opacity .6s; }
  .hue-hero-art.show { opacity: .55; }
  .hue-hero-wash { position: absolute; inset: -20%; z-index: 0; filter: blur(8px); transition: opacity .4s; }
  .hue-hero-bars { position: absolute; left: 0; right: 0; bottom: 0; height: 64px; z-index: 0; opacity: .55;
    mask: linear-gradient(to top, #000, transparent); -webkit-mask: linear-gradient(to top, #000, transparent); padding: 0 6px; }
  .hue-hero-top { position: relative; z-index: 2; display: flex; align-items: center; justify-content: space-between; }
  .hue-hero-pills { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .hue-pill { display: inline-flex; align-items: center; gap: 7px; padding: 6px 12px; border-radius: 999px; background: #00000040;
    backdrop-filter: blur(6px); border: 1px solid var(--hue-line); font-size: 12px; font-weight: 700; letter-spacing: .01em; }
  .hue-pill-dot { width: 7px; height: 7px; border-radius: 50%; }
  .hue-pill-label { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hue-hero-now { position: relative; z-index: 2; display: flex; align-items: stretch; gap: 14px; margin-top: 22px; }
  /* Right column fills the cover's height: title/artist pinned to the top
     (aligned with the cover's top edge) and the transport pinned to the
     bottom (aligned with the cover's bottom edge), so the block reads as one
     compact unit instead of pushing the artwork/title up. */
  .hue-now-right { flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: space-between; gap: 10px; }
  .hue-now-toprow { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; min-width: 0; }
  .hue-bright-mini { display: inline-flex; align-items: center; gap: 5px; padding: 7px 11px; border-radius: 11px; background: #00000040;
    backdrop-filter: blur(6px); border: 1px solid var(--hue-line); font-size: 13px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .hue-bright-mini-icon { font-size: 12px; }
  .hue-amb-body { position: relative; padding: 16px 20px 20px; background: linear-gradient(180deg, #121120cc, #0f0e1c); }

  /* -- transport row (lives at the bottom of the now-playing right column,
        aligned to the cover's bottom edge; the buttons flex to fill the band
        and the time readout sits at the right edge) -- */
  .hue-transport { position: relative; z-index: 2; display: flex; align-items: center; gap: 8px; }
  .hue-tr-btn { flex: 1; min-width: 0; height: 42px; border-radius: 13px; border: 1px solid var(--hue-line);
    background: #00000040; color: var(--hue-text); cursor: pointer; transition: .15s;
    display: inline-flex; align-items: center; justify-content: center; backdrop-filter: blur(6px);
    touch-action: manipulation; }
  .hue-tr-btn:hover { background: #ffffff18; }
  .hue-tr-btn:active { transform: scale(.94); }
  .hue-tr-btn.main { flex: 1.35; background: #ffffff14; }
  .hue-tr-time { flex: none; margin-left: 4px; min-width: 46px; text-align: right;
    font-size: 11px; font-weight: 700; color: var(--hue-dim);
    font-variant-numeric: tabular-nums; letter-spacing: .02em; }

  /* -- title marquee (long titles scroll once into view) -- */
  .hue-now-track { text-shadow: 0 1px 10px #000a; }
  .hue-now-track-inner { display: inline-block; white-space: nowrap; }
  .hue-now-track-inner.scroll { animation: hue-mq 9s linear infinite alternate; }
  @keyframes hue-mq { 0%, 18% { transform: translateX(0); } 82%, 100% { transform: translateX(var(--mq, 0px)); } }

  /* -- song-structure timeline (energy silhouette + playhead) -- */
  .hue-tl { position: relative; z-index: 2; margin-top: 14px; height: 22px; display: none; }
  .hue-tl.live { display: block; }
  .hue-tl-sec { position: absolute; bottom: 0; border-radius: 3px 3px 0 0; transition: filter .3s, opacity .3s; }
  .hue-tl-sec.past { opacity: .45; }
  .hue-tl-sec.arming { animation: hue-arm 0.9s ease-in-out infinite; }
  .hue-tl-marker { position: absolute; top: -2px; bottom: -2px; width: 2px; border-radius: 2px;
    background: #fff; box-shadow: 0 0 7px #ffffffaa; }
  @keyframes hue-arm { 0%, 100% { filter: brightness(1); } 50% { filter: brightness(1.9); } }

  /* -- room mirror (live lamp stage) -- */
  .hue-stage { position: relative; height: 92px; border-radius: 14px; margin-bottom: 14px;
    background: radial-gradient(120% 160% at 50% 120%, #ffffff08, transparent 60%), #00000044;
    border: 1px solid var(--hue-line); overflow: hidden; display: none; }
  .hue-stage.live { display: block; }
  .hue-stage-dot { position: absolute; width: 15px; height: 15px; border-radius: 50%;
    transform: translate(-50%, -50%); background: #1c1b2e;
    transition: background .09s linear, box-shadow .09s linear; }
  .hue-stage-dot.swap { animation: hue-swap .5s ease; }
  @keyframes hue-swap { 0% { transform: translate(-50%,-50%) scale(1); } 45% { transform: translate(-50%,-50%) scale(1.55); } 100% { transform: translate(-50%,-50%) scale(1); } }
  .hue-stage-tag { position: absolute; left: 10px; top: 7px; display: inline-flex; align-items: center; gap: 5px;
    font-size: 9.5px; font-weight: 800; letter-spacing: .14em; color: var(--hue-dim); text-transform: uppercase; }
  .hue-stage-tag-dot { width: 6px; height: 6px; border-radius: 50%; background: #ff4b5c; animation: hue-blink 1.6s ease-in-out infinite; }
  @keyframes hue-blink { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }
  .hue-stage-legend { position: absolute; right: 10px; top: 7px; display: flex; gap: 6px; align-items: center;
    font-size: 9.5px; font-weight: 700; letter-spacing: .08em; color: var(--hue-faint); text-transform: uppercase; }
  .hue-stage-legend span { display: inline-flex; align-items: center; }
  .hue-stage-legend .hue-stage-spectrum { width: 52px; height: 6px; border-radius: 4px; display: inline-block; }

  /* -- idle beauty: slow palette lava drift while paused -- */
  .hue-hero-wash.idle { animation: hue-lava 26s ease-in-out infinite alternate; }
  @keyframes hue-lava {
    0% { filter: blur(8px) hue-rotate(0deg); transform: scale(1) translateY(0); }
    100% { filter: blur(8px) hue-rotate(38deg); transform: scale(1.09) translateY(-2.5%); }
  }

  /* -- beat pads overlay (full-page: tall tap columns) -- */
  .hue-drum { position: absolute; inset: 0; z-index: 10; display: flex; flex-direction: column;
    gap: 12px; border-radius: 26px; padding: 16px 16px 12px; overflow-y: auto;
    background: #0b0a14f2; backdrop-filter: blur(8px); user-select: none; -webkit-user-select: none; }
  .hue-drum-head { display: flex; align-items: center; gap: 12px; flex: none; }
  .hue-drum-back { width: 38px; height: 38px; border-radius: 12px; border: 1px solid var(--hue-line);
    background: #ffffff0a; color: var(--hue-text); cursor: pointer; flex: none;
    display: inline-flex; align-items: center; justify-content: center; transition: .15s; }
  .hue-drum-back:hover { background: #ffffff16; }
  .hue-drum-titles { display: flex; flex-direction: column; min-width: 0; }
  .hue-cal-title { font-size: 16px; font-weight: 800; letter-spacing: .02em; }
  .hue-drum-track { font-size: 11.5px; color: var(--hue-dim); font-weight: 600;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hue-cal-sub { font-size: 12.5px; color: var(--hue-dim); text-align: center; flex: none; }
  .hue-cal-cancel { margin-top: 2px; font-size: 11px; color: var(--hue-faint); text-transform: uppercase;
    letter-spacing: .1em; cursor: pointer; text-align: center; flex: none; }
  .hue-drum-pads { display: flex; gap: 12px; width: 100%; flex: 1 1 auto; min-height: 200px; }
  .hue-drum-pad { flex: 1; min-width: 0; border-radius: 16px; cursor: pointer; position: relative;
    border: 1px solid var(--hue-line); color: #fff; font-family: var(--hk); padding: 16px 8px 14px;
    background: linear-gradient(180deg, #14131f, #0c0b16); overflow: hidden;
    box-shadow: inset 0 0 0 1px #ffffff10;
    display: flex; flex-direction: column; align-items: center; justify-content: space-between;
    transition: transform .06s, filter .12s; touch-action: manipulation; }
  .hue-drum-pad:disabled { opacity: .45; cursor: default; }
  .hue-drum-pad:active { transform: scale(.97); }
  .hue-drum-tap { font-size: 10px; font-weight: 800; letter-spacing: .3em; text-indent: .3em;
    color: var(--hue-faint); text-transform: uppercase; }
  .hue-drum-mid { display: flex; flex-direction: column; align-items: center; gap: 16px; }
  .hue-drum-glyph { display: flex; align-items: flex-end; gap: 3px; height: 32px; }
  .hue-drum-glyph span { width: 5px; border-radius: 2px; background: var(--pad); opacity: .9; }
  .hue-drum-dots { display: flex; gap: 4px; }
  .hue-drum-dots span { width: 4px; height: 4px; border-radius: 50%; background: #ffffff26; }
  .hue-drum-glow { position: absolute; left: 0; right: 0; bottom: 0; height: 46%; pointer-events: none;
    background: radial-gradient(130% 105% at 50% 118%, var(--pad), transparent 72%);
    opacity: .45; transition: opacity .15s; }
  .hue-drum-pad.hit .hue-drum-glow { opacity: .95; }
  .hue-drum-lab { position: relative; display: flex; flex-direction: column; align-items: center; gap: 2px; }
  .hue-drum-pad-label { font-size: 12px; font-weight: 800; letter-spacing: .18em; text-indent: .18em;
    text-transform: uppercase; text-shadow: 0 1px 6px #000a; }
  .hue-drum-pad-sub { font-size: 10.5px; color: var(--hue-dim); font-weight: 600; }
  .hue-drum-pad.hit { animation: hue-drum-hit .42s ease-out; }
  @keyframes hue-drum-hit {
    0% { filter: brightness(1.9); transform: scale(1.05); }
    100% { filter: brightness(1); transform: scale(1); }
  }

  /* -- intensity preview micro-animations -- */
  .hue-seg-anim { position: absolute; left: 50%; bottom: 3px; transform: translateX(-50%);
    width: 18px; height: 3px; border-radius: 2px; opacity: .8; pointer-events: none; }
  .hue-seg-anim.m-auto { background: linear-gradient(90deg, #27d3ff, #7b5cff, #ff7ab8, #27d3ff); background-size: 300% 100%; animation: hue-pv-drift 2.6s linear infinite; }
  .hue-seg-anim.m-subtle { background: linear-gradient(90deg, #ff7ab8, #7b5cff, #27d3ff); background-size: 300% 100%; animation: hue-pv-drift 4s linear infinite; }
  .hue-seg-anim.m-medium { background: currentColor; animation: hue-pv-breathe 1.4s ease-in-out infinite; }
  .hue-seg-anim.m-high { background: currentColor; animation: hue-pv-trio 1.1s ease-in-out infinite; }
  .hue-seg-anim.m-intense { background: currentColor; animation: hue-pv-snap .55s ease-out infinite; }
  .hue-seg-anim.m-extreme { background: currentColor; animation: hue-pv-strobe .3s steps(2, jump-none) infinite; }
  @keyframes hue-pv-drift { 0% { background-position: 0% 0; } 100% { background-position: 300% 0; } }
  @keyframes hue-pv-breathe { 0%, 100% { opacity: .35; } 50% { opacity: .9; } }
  @keyframes hue-pv-trio { 0%, 100% { clip-path: inset(0 66% 0 0); } 33% { clip-path: inset(0 33% 0 33%); } 66% { clip-path: inset(0 0 0 66%); } }
  @keyframes hue-pv-snap { 0% { opacity: 1; } 60% { opacity: .15; } 100% { opacity: .15; } }
  @keyframes hue-pv-strobe { 0% { opacity: 1; } 100% { opacity: .08; } }

  @media (prefers-reduced-motion: reduce) {
    .hue-stage-dot.swap, .hue-tl-sec.arming, .hue-now-track-inner.scroll,
    .hue-hero-wash.idle, .hue-seg-anim, .hue-stage-tag-dot { animation: none !important; }
  }
`;

/* ------------------------- Visualizer -------------------------
   Ambient bars driven by the *real* audio analysis when the integration's live
   WebSocket feed is connected (band energies + kick flags at ~20 Hz), falling
   back to a tempo/position-locked simulation (bpm + beat anchor) when it isn't
   - so the bars are the actual music whenever they can be. */
class Viz {
  constructor(count) {
    this.count = count;
    this.levels = new Array(count).fill(0.06);
    this.beat = 0;
    this.downbeat = 0; // bigger breath on the 1 of each bar
    this.energy = 0;
    this._beat = 0;
    this._down = 0;
    this._lastBeat = 0;
    this._liveBeats = 0;
  }
  step(active, time, bpm, beatAnchor, live) {
    const t = time;
    this._down *= 0.9;
    this.downbeat = active ? this._down : 0;
    if (live && active) {
      this._stepLive(t, live);
      return;
    }
    const tempo = bpm && bpm > 0 ? bpm : 122;
    // `beatAnchor` is a real beat time (seconds, on the playback timeline) the
    // integration detected, so the grid lands on the actual downbeats instead of
    // assuming beat 0 sits at position 0. Falls back to 0 when not provided.
    const anchor = Number.isFinite(beatAnchor) ? beatAnchor : 0;
    const beatPhase = ((t - anchor) * tempo) / 60;
    const sinceBeat = beatPhase - Math.floor(beatPhase);
    const idx = Math.floor(beatPhase);
    if (idx !== this._lastBeat) {
      this._lastBeat = idx;
      if (active) {
        this._beat = 1;
        if (((idx % 4) + 4) % 4 === 0) this._down = 1; // the bar's downbeat
      }
    }
    this._beat *= 0.86;
    const beat = active ? this._beat : 0;

    let sum = 0;
    for (let i = 0; i < this.count; i++) {
      const f = i / this.count;
      const bass = Math.pow(1 - f, 1.6);
      const treble = Math.pow(f, 1.4);
      const shimmer = 0.5 + 0.5 * Math.sin(t * (5 + f * 9) + i * 1.7);
      const wob = 0.5 + 0.5 * Math.sin(t * (1.3 + f * 2) + i);
      let v = active
        ? 0.14 + bass * beat * 0.8 + treble * shimmer * 0.32 + wob * 0.12 * (1 - sinceBeat)
        : 0.05 + 0.015 * Math.sin(t * 1.1 + i);
      v = Math.max(0.04, Math.min(1, v));
      this.levels[i] = v;
      sum += v;
    }
    this.beat = beat;
    this.downbeat = active ? this._down : 0;
    this.energy = active ? sum / this.count : 0;
  }
  _stepLive(t, live) {
    // Kick pulses come from the integration's bass-onset stream; fire each
    // event exactly once (the same object arrives for several frames).
    if (live.beat && !live._beatSeen) {
      live._beatSeen = true;
      this._beat = Math.max(this._beat, Math.min(1, (live.strength || 1.5) / 2));
      this._liveBeats += 1;
      if (this._liveBeats % 4 === 1) this._down = 1; // approximate bar pulse
    }
    this._beat *= 0.88;
    const bands = live.bands || [];
    const top = bands.length - 1;
    for (let i = 0; i < this.count; i++) {
      const f = i / Math.max(1, this.count - 1);
      // Interpolate the 5 analysed bands across the bars, with a gentle
      // per-bar texture so neighbours aren't carbon copies.
      const x = f * top;
      const lo = Math.floor(x);
      const hi = Math.min(top, lo + 1);
      const band = (bands[lo] || 0) + ((bands[hi] || 0) - (bands[lo] || 0)) * (x - lo);
      const texture = 0.82 + 0.18 * Math.sin(t * (4 + f * 8) + i * 1.7);
      // Bass-weighted kick pulse so the low end visibly slams on the beat.
      const kick = this._beat * Math.pow(1 - f, 1.5) * 0.45;
      const v = Math.max(0.05, Math.min(1, 0.07 + band * 0.9 * texture + kick));
      this.levels[i] = v;
    }
    this.beat = this._beat;
    this.downbeat = this._down;
    this.energy = live.energy != null ? live.energy : 0.5;
  }
}

/* Advanced live tunables: knobs shown under the intensity when Advanced is on.
   Each is a 0-200% multiplier on the active mode's params (100% = as designed);
   the integration sanitises + applies them live. Keys match const.TUNABLE_KEYS. */
const TUNABLE_DEFS = [
  { key: "reactivity",   label: "Reactivity",   icon: "⚡" },       // flash punch
  { key: "glow",         label: "Glow",         icon: "\u{1F506}" },    // room brightness
  { key: "movement",     label: "Movement",     icon: "↻" },       // spatial motion
  { key: "contrast",     label: "Contrast",     icon: "◐" },       // small vs big peaks
  { key: "colour_speed", label: "Colour speed", icon: "\u{1F308}" },    // colour drift
  { key: "loudness",     label: "Loudness",     icon: "\u{1F50A}" },    // per-band loudness
];
const TUNABLE_KEYS = TUNABLE_DEFS.map((d) => d.key);
// True when two tunable maps are equal across all keys (missing == 100%).
function tunEq(a, b) {
  a = a || {}; b = b || {};
  return TUNABLE_KEYS.every((k) => (a[k] == null ? 1 : a[k]) === (b[k] == null ? 1 : b[k]));
}

/* ------------------------- The card element ------------------------- */
class HueMusicSyncCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null;
    this._areas = [];
    this._areaIndex = 0;
    this._demo = false;
    this._areaMenuOpen = false;   // is the area dropdown expanded?
    this._playerMenuOpen = false; // is the player dropdown expanded?
    this._autoMenuOpen = false;   // is the Auto-rungs checklist expanded?
    this._autoLevelsDirty = false; // a local rung toggle awaiting the echo-back
    this._autoTimingDirty = false; // a local auto-timing toggle awaiting echo-back
    this._areaAutoDone = false;   // have we defaulted the view to the active area yet?
    // Close the dropdowns when a pointer lands outside them. One persistent
    // document listener (added on connect) rather than per-open wiring, since a
    // re-render rebuilds the menu nodes; composedPath crosses the shadow boundary.
    this._onDocPointer = (e) => {
      if (!this._areaMenuOpen && !this._playerMenuOpen && !this._autoMenuOpen) return;
      const path = e.composedPath ? e.composedPath() : [];
      if (path.some((n) => n.classList && n.classList.contains("hue-area-dd"))) return;
      this._areaMenuOpen = false;
      this._playerMenuOpen = false;
      this._autoMenuOpen = false;
      this._render();
    };
    // The menus are position:fixed (anchored to their triggers), so a page
    // scroll or resize would leave them floating detached; close them instead,
    // like a native <select>. Scrolls *inside* a menu itself are fine.
    this._onWinMove = (e) => {
      if (!this._areaMenuOpen && !this._playerMenuOpen && !this._autoMenuOpen) return;
      const path = e && e.composedPath ? e.composedPath() : [];
      if (path.some((n) => n.classList && n.classList.contains("hue-area-menu"))) return;
      this._areaMenuOpen = false;
      this._playerMenuOpen = false;
      this._autoMenuOpen = false;
      this._render();
    };

    // local optimistic UI state (only used in demo / before hass arrives)
    this._ui = {
      on: true,
      intensity: "Intense",
      effect: "Music",
      colour: "aurora",
      brightness: 64,
      timing: -60,
      autoLevels: ["subtle", "medium", "high"],
      autoTiming: false,
    };

    this._viz = new Viz(40);
    this._barNodes = null;
    this._raf = 0;
    this._dragging = false;

    this._albumColors = null; // client-extracted album palette (fallback)
    this._artKey = null;      // album-art URL we last extracted from
    this._play = null;        // playback timing snapshot for the viz loop

    // Album-art display pipeline: URLs are preloaded before being applied so a
    // stale/broken URL keeps the placeholder instead of an empty dark tile.
    this._coverArtNode = null; // the cover tile's art layer (this render)
    this._heroArtNode = null;  // the blurred hero background layer (this render)
    this._artGoodUrl = null;   // last URL that actually loaded
    this._artWanted = null;    // URL currently loading/desired

    // Live feed from the integration (real analysis + lamp mirror + sections).
    this._live = null;        // latest "stream" event (~20 Hz)
    this._liveMeta = null;    // latest "meta" event (~1 Hz)
    this._liveSubFor = null;  // switch entity we're subscribed for
    this._liveUnsub = null;   // promise of the unsubscribe fn
    this._liveRetryAt = 0;    // backoff after a failed subscribe
    this._stageNode = null;   // room-mirror panel (this render)
    this._stageDots = null;   // cid -> dot node
    this._stageRoles = "";    // last applied role signature
    this._stageSig = "";      // last applied positions signature
    this._tlNode = null;      // timeline panel (this render)
    this._tlSecs = null;      // section block nodes
    this._tlMarker = null;
    this._tlSig = "";         // last applied sections signature
    this._curSec = null;      // current timeline section (drop detection)
    this._bloom = 0;          // one-shot card bloom on a section drop
    this._trTime = null;      // transport time readout node
    this._trDur = 0;
    this._marqueeNodes = null;
    this._visible = true;     // IntersectionObserver gate for the rAF loop
    this._reduced =
      typeof matchMedia === "function" &&
      matchMedia("(prefers-reduced-motion: reduce)").matches;
    this._drum = null;        // drum-pad ("play the beats") overlay state

    // Player picker: which player the integration should follow. The candidate
    // list is dynamic (players start and stop), so it's fetched on demand and
    // only polled while the picker sheet is actually open.
    this._players = null;     // last hue_music_sync/players result
    this._playersFor = null;  // switch entity the list belongs to
    this._playersAt = 0;      // when it was fetched (ms)
    this._playersReq = false; // a fetch is in flight
  }

  /* -- config -- */
  setConfig(config) {
    // Guard: the picker/editor can call with no config at all; touching
    // `config.areas` on null would throw and Lovelace renders that as a
    // red "Configuration error" card.
    config = config || {};
    this._config = config;
    // Normalise area definitions.
    if (Array.isArray(config.areas) && config.areas.length) {
      this._areas = config.areas.map((a) => ({
        name: a.name || titleize((a.switch || "").split(".").pop() || "Area"),
        switch: a.switch,
        intensity: a.intensity,
        effect: a.effect,
        colour: a.colour || a.color,
        brightness: a.brightness,
        timing: a.timing,
        media_player: a.media_player || config.media_player,
      }));
    } else if (config.switch || config.intensity || config.colour) {
      // single flat area
      this._areas = [{
        name: config.name || "Music Sync",
        switch: config.switch,
        intensity: config.intensity,
        effect: config.effect,
        colour: config.colour || config.color,
        brightness: config.brightness,
        timing: config.timing,
        media_player: config.media_player,
      }];
    } else {
      // no entities -> demo mode
      this._areas = DEMO_AREAS.map((a) => ({ name: a.name, demo: true }));
      this._demo = true;
    }
    this._areaIndex = Math.min(this._areaIndex, this._areas.length - 1);
    this._maybeSelectActiveArea(); // if hass is already here, open on the live area
    this._render();
  }

  set hass(hass) {
    const prev = this._hass;
    this._hass = hass;
    // On first load, open on whichever area is actually syncing rather than
    // always the first one (may change _areaIndex, so force a render if it does).
    const areaMoved = this._maybeSelectActiveArea();
    // HA sets `hass` on every global state change; only re-render when an entity
    // this card actually shows has changed (or on first assignment / mid-config).
    const sig = this._signature(hass);
    if (!areaMoved && prev && sig === this._sig && !this._dragging) return;
    this._sig = sig;
    if (!this._dragging) this._render();
  }
  get hass() { return this._hass; }

  // Point the card at the active (syncing) area the first time we can tell which
  // one it is, so opening the dashboard shows what's actually running instead of
  // whichever area happens to be first. Runs once; a manual pick pins it after.
  // Returns true if it changed the selected area.
  _maybeSelectActiveArea() {
    if (this._areaAutoDone || this._demo || !this._hass || this._areas.length < 2) {
      return false;
    }
    let anyKnown = false;
    let activeIdx = -1;
    this._areas.forEach((a, i) => {
      if (!a.switch) return;
      const e = this._hass.states[a.switch];
      if (!e) return;
      anyKnown = true;
      if (activeIdx < 0 && e.state === "on") activeIdx = i;
    });
    if (!anyKnown) return false; // switch entities not loaded yet; try next update
    this._areaAutoDone = true;
    if (activeIdx >= 0 && activeIdx !== this._areaIndex) {
      this._areaIndex = activeIdx;
      return true;
    }
    return false;
  }

  // Cheap fingerprint of the entities the card depends on across all areas.
  _signature(hass) {
    if (!hass) return "";
    let out = "";
    const npSig = (e) => {
      if (!e) return "\u2205";
      const x = e.attributes;
      return `${e.state}|${x.media_title || ""}|${x.media_artist || ""}|${x.entity_picture || x.media_image || ""}` +
        `|${x.media_position || ""}|${x.bpm || ""}|${x.album_colors ? JSON.stringify(x.album_colors) : (x.palette ? JSON.stringify(x.palette) : "")}`;
    };
    for (const a of this._areas) {
      for (const id of [a.intensity, a.effect, a.colour, a.brightness, a.timing]) {
        if (!id) continue;
        const e = hass.states[id];
        out += e ? `${id}=${e.state};` : `${id}=\u2205;`;
      }
      // switch carries the area state plus any integration-published now-playing /
      // album-colour / bpm attributes, so re-render when those change.
      if (a.switch) {
        const swe = hass.states[a.switch];
        out += `${a.switch}=${npSig(swe)};`;
        // The live player the integration follows (for artwork/title updates).
        const src = swe && swe.attributes.source_player;
        if (src) out += `${src}=${npSig(hass.states[src])};`;
      }
      if (a.media_player) out += `${a.media_player}=${npSig(hass.states[a.media_player])};`;
    }
    return out;
  }

  static getStubConfig() {
    return {
      type: "custom:hue-music-sync-card",
      areas: [
        {
          name: "Living Room",
          switch: "switch.music_sync_living_room",
          intensity: "select.music_sync_living_room_intensity",
          effect: "select.music_sync_living_room_effect",
          colour: "select.music_sync_living_room_colour",
          brightness: "number.music_sync_living_room_brightness",
          timing: "number.music_sync_living_room_timing_offset",
          media_player: "media_player.living_room",
        },
      ],
    };
  }

  getCardSize() { return 6; }

  connectedCallback() {
    this._loop = this._loop.bind(this);
    // Cancel any loop left scheduled from a previous attach, so a detach/re-attach
    // (HA moves cards around the DOM) can never leave two rAF loops running.
    cancelAnimationFrame(this._raf);
    this._raf = requestAnimationFrame(this._loop);
    // Don't animate when the card is scrolled out of view (wall tablets often
    // keep dashboards open 24/7; the browser pauses rAF only for hidden tabs).
    if ("IntersectionObserver" in window && !this._io) {
      this._visible = true;
      this._io = new IntersectionObserver((entries) => {
        for (const e of entries) this._visible = e.isIntersecting;
      });
      this._io.observe(this);
    }
    document.addEventListener("pointerdown", this._onDocPointer, true);
    window.addEventListener("scroll", this._onWinMove, { capture: true, passive: true });
    window.addEventListener("resize", this._onWinMove, { passive: true });
  }
  disconnectedCallback() {
    cancelAnimationFrame(this._raf);
    document.removeEventListener("pointerdown", this._onDocPointer, true);
    window.removeEventListener("scroll", this._onWinMove, { capture: true });
    window.removeEventListener("resize", this._onWinMove);
    this._areaMenuOpen = false;
    this._playerMenuOpen = false;
    this._autoMenuOpen = false;
    if (this._io) {
      this._io.disconnect();
      this._io = null;
    }
    this._dropLiveSub();
  }

  /* -- live feed subscription -- */
  _ensureLiveSub() {
    const area = this._areas[this._areaIndex] || {};
    const sw = area.switch;
    const conn = this._hass && this._hass.connection;
    if (!conn || !sw || this._demo) return;
    if (this._liveSubFor === sw) return;
    this._dropLiveSub();
    if (Date.now() < this._liveRetryAt) return;
    this._liveSubFor = sw;
    try {
      this._liveUnsub = conn.subscribeMessage((ev) => this._onLive(ev), {
        type: "hue_music_sync/subscribe",
        entity_id: sw,
      });
      this._liveUnsub.catch(() => {
        // Older integration / switch not registered yet: retry later, the
        // simulated visualizer keeps running meanwhile.
        if (this._liveSubFor === sw) {
          this._liveSubFor = null;
          this._liveUnsub = null;
          this._liveRetryAt = Date.now() + 30000;
        }
      });
    } catch (_) {
      this._liveSubFor = null;
      this._liveUnsub = null;
      this._liveRetryAt = Date.now() + 30000;
    }
  }

  _dropLiveSub() {
    const unsub = this._liveUnsub;
    this._liveUnsub = null;
    this._liveSubFor = null;
    this._live = null;
    this._liveMeta = null;
    if (unsub && unsub.then) {
      unsub.then((f) => { try { f && f(); } catch (_) {} }).catch(() => {});
    }
  }

  _onLive(ev) {
    if (!ev || !ev.type) return;
    if (ev.type === "stream") {
      ev.at = performance.now();
      this._live = ev;
    } else if (ev.type === "meta") {
      ev.at = Date.now();
      this._liveMeta = ev;
      this._syncStage(ev);
      this._syncTimeline(ev);
    }
  }

  _liveFresh() {
    const l = this._live;
    return l && performance.now() - l.at < 450 ? l : null;
  }

  /* -- derive the live model for the active area -- */
  _model() {
    const area = this._areas[this._areaIndex] || {};
    const hass = this._hass;
    const st = (id) => (hass && id && hass.states[id]) || null;

    // helpers to build option lists from a select entity
    const selectModel = (id, fallback) => {
      const e = st(id);
      if (e && Array.isArray(e.attributes.options) && e.attributes.options.length) {
        return {
          entity: id,
          value: e.state,
          options: e.attributes.options.map((o) => ({ value: o, label: titleize(o) })),
        };
      }
      return {
        entity: id || null,
        value: null,
        options: fallback.map((o) => ({ value: o, label: o })),
      };
    };

    const sw = st(area.switch);
    const intensity = selectModel(area.intensity, DEFAULT_INTENSITIES);
    const effect = selectModel(area.effect, DEFAULT_EFFECTS);

    // colour
    const colourEnt = st(area.colour);
    let colourOptions, colourValue, colourEntity;
    if (colourEnt && Array.isArray(colourEnt.attributes.options) && colourEnt.attributes.options.length) {
      colourEntity = area.colour;
      colourValue = colourEnt.state;
      colourOptions = colourEnt.attributes.options.map((o) => {
        const sw2 = matchPalette(o);
        return { value: o, name: sw2 ? sw2.name : titleize(o), colors: sw2 ? sw2.colors : DEFAULT_SWATCH, album: !!(sw2 && sw2.album) };
      });
    } else {
      colourEntity = null;
      colourValue = null;
      colourOptions = PALETTES.map((p) => ({ value: p.id, name: p.name, colors: p.colors, album: !!p.album }));
    }

    const brightEnt = st(area.brightness);
    const timingEnt = st(area.timing);
    const mp = st(area.media_player);
    const swAttr = sw ? sw.attributes : {};
    const mpAttr = mp ? mp.attributes : {};

    // Album colours: prefer what the integration extracted (published on the
    // switch/media_player), else the client-side extraction from the art, else
    // the static default. The "Album colours" swatch reflects whichever wins.
    const integColors =
      parseColorList(swAttr.album_colors) ||
      parseColorList(swAttr.palette) ||
      parseColorList(mpAttr.album_colors);
    const albumColors = integColors || this._albumColors;
    if (albumColors && albumColors.length) {
      colourOptions = colourOptions.map((o) =>
        o.album ? { ...o, colors: albumColors } : o
      );
    }

    // resolve current values, preferring live entity state, falling back to local UI.
    const on = sw ? sw.state === "on" : this._ui.on;
    const intensityVal = intensity.value != null ? intensity.value
      : (intensity.options.find((o) => o.value === this._ui.intensity)?.value ?? intensity.options[0].value);
    const effectVal = effect.value != null ? effect.value
      : (effect.options.find((o) => o.value === this._ui.effect)?.value ?? effect.options[0].value);
    const colourVal = colourValue != null ? colourValue
      : (colourOptions.find((o) => o.value === this._ui.colour)?.value ?? colourOptions[0].value);

    const selColour = colourOptions.find((o) => o.value === colourVal) || colourOptions[0];
    const accent = (selColour.colors && selColour.colors[0]) || DEFAULT_SWATCH[0];

    const brightVal = brightEnt ? Number(brightEnt.state) : this._ui.brightness;
    const brightMin = brightEnt ? Number(brightEnt.attributes.min ?? 5) : 5;
    const brightMax = brightEnt ? Number(brightEnt.attributes.max ?? 100) : 100;

    const timingVal = timingEnt ? Number(timingEnt.state) : this._ui.timing;
    const timingMin = timingEnt ? Number(timingEnt.attributes.min ?? -200) : -200;
    const timingMax = timingEnt ? Number(timingEnt.attributes.max ?? 200) : 200;
    const timingStep = timingEnt ? Number(timingEnt.attributes.step ?? 5) : 5;

    // now playing - prefer the *live* player the integration is actually
    // following (published as `source_player` on the switch; zero config),
    // then a configured media_player, then the switch's mirrored attributes,
    // then demo data. Reading the live entity matters for artwork: the
    // mirrored `media_image` is a tokenised proxy URL that can go stale.
    const posOf = (a) => ({
      position: Number(a.media_position || 0),
      updatedAt: a.media_position_updated_at ? Date.parse(a.media_position_updated_at) : Date.now(),
    });
    const live = st(swAttr.source_player) || mp;
    let now;
    if (live) {
      const la = live.attributes;
      now = {
        track: la.media_title || swAttr.media_title || titleize(live.state) || "-",
        artist: la.media_artist || la.media_album_name || swAttr.media_artist || "",
        art: la.entity_picture || swAttr.media_image || null,
        playing: live.state === "playing",
        player: live.entity_id,
        duration: Number(la.media_duration || 0) || 0,
        ...posOf(la.media_position != null ? la : swAttr),
      };
    } else if (sw && (swAttr.media_title || swAttr.entity_picture || swAttr.media_image)) {
      now = {
        track: swAttr.media_title || "-",
        artist: swAttr.media_artist || "",
        art: swAttr.entity_picture || swAttr.media_image || null,
        playing: on,
        player: null,
        duration: 0,
        ...posOf(swAttr),
      };
    } else {
      now = {
        track: DEMO_NOW.track, artist: DEMO_NOW.artist, art: null, playing: on,
        player: null, duration: 0, position: 0, updatedAt: Date.now(),
      };
    }

    const bpm = Number(swAttr.bpm || mpAttr.bpm || 0) || 0;
    const ba = swAttr.beat_anchor ?? mpAttr.beat_anchor;
    const beatAnchor = ba != null && Number.isFinite(Number(ba)) ? Number(ba) : null;

    // When Auto intensity is active, the integration publishes the rung it
    // resolved to from the music (subtle..extreme) so we can show it, plus the
    // enabled set the picker may choose from (the checklist below the control).
    const autoMode = intensityVal === "auto" && swAttr.auto_mode
      ? String(swAttr.auto_mode) : null;
    const liveAutoLevels =
      Array.isArray(swAttr.auto_levels) && swAttr.auto_levels.length
        ? swAttr.auto_levels.map((s) => String(s)) : null;
    // Optimistic: after a local toggle we keep showing the pending value until
    // the integration echoes it back (both lists are ladder-ordered, so a plain
    // join compares them), then live takes over again. Keeps the checkmarks
    // instant without fighting the ~1 Hz attribute publish.
    if (liveAutoLevels) {
      if (this._autoLevelsDirty
          && liveAutoLevels.join(",") === (this._ui.autoLevels || []).join(",")) {
        this._autoLevelsDirty = false;
      }
      if (!this._autoLevelsDirty) this._ui.autoLevels = liveAutoLevels;
    }
    const autoLevels = this._ui.autoLevels || liveAutoLevels
      || ["subtle", "medium", "high"];

    // Auto timing: on/off (optimistic like the rungs), plus the live calibrated
    // correction and whether it has locked for this song.
    if (swAttr.auto_timing != null) {
      if (this._autoTimingDirty && !!swAttr.auto_timing === !!this._ui.autoTiming) {
        this._autoTimingDirty = false;
      }
      if (!this._autoTimingDirty) this._ui.autoTiming = !!swAttr.auto_timing;
    }
    const autoTiming = !!this._ui.autoTiming;
    const timingAutoMs = swAttr.timing_auto_ms != null ? Number(swAttr.timing_auto_ms) : null;
    const timingLocked = !!swAttr.timing_locked;

    // Advanced tunables: the toggle + the live knob factors, both optimistic
    // (kept until the integration echoes them back) like the auto rungs/timing.
    if (swAttr.advanced != null) {
      if (this._advDirty && !!swAttr.advanced === !!this._ui.advanced) this._advDirty = false;
      if (!this._advDirty) this._ui.advanced = !!swAttr.advanced;
    }
    const advanced = !!this._ui.advanced;
    const liveTun = swAttr.tunables || {};
    if (!this._tunDirty) this._ui.tunables = Object.assign({}, liveTun);
    else if (tunEq(this._ui.tunables, liveTun)) this._tunDirty = false;
    const tunables = {};
    for (const k of TUNABLE_KEYS) {
      const uiv = this._ui.tunables && this._ui.tunables[k];
      tunables[k] = uiv != null ? uiv : (liveTun[k] != null ? liveTun[k] : 1.0);
    }

    return {
      area, on,
      intensity: { ...intensity, value: intensityVal, autoMode, autoLevels },
      effect: { ...effect, value: effectVal },
      colour: {
        entity: colourEntity, value: colourVal, options: colourOptions,
        selected: selColour, albumFromIntegration: !!integColors,
      },
      accent, bpm, beatAnchor,
      brightness: { entity: area.brightness, value: brightVal, min: brightMin, max: brightMax },
      timing: {
        entity: area.timing, value: timingVal, min: timingMin, max: timingMax,
        step: timingStep, auto: autoTiming, autoMs: timingAutoMs, locked: timingLocked,
      },
      now,
      advanced, tunables,
      audioSource: swAttr.audio_source || null,
    };
  }

  /* -- album-art colour extraction (client-side fallback) -- */
  _maybeExtractAlbum(m) {
    // If the integration already publishes album colours, never extract.
    if (m.colour && m.colour.albumFromIntegration) { this._artKey = null; return; }
    const art = m.now.art;
    if (!art) { this._albumColors = null; this._artKey = null; return; }
    if (art === this._artKey) return;
    this._artKey = art;
    this._extractAlbumColors(art);
  }

  _extractAlbumColors(url) {
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => {
      if (url !== this._artKey) return; // track moved on while loading
      try {
        const n = 28;
        const cv = document.createElement("canvas");
        cv.width = n; cv.height = n;
        const ctx = cv.getContext("2d", { willReadFrequently: true });
        ctx.drawImage(img, 0, 0, n, n);
        const cols = extractVibrant(ctx.getImageData(0, 0, n, n).data, 3);
        if (cols && cols.length) { this._albumColors = cols; this._render(); }
      } catch (_) {
        /* cross-origin art taints the canvas; keep the current palette */
      }
    };
    img.onerror = () => {};
    img.src = url;
  }

  /* -- service calls (no-op in demo) -- */
  _callSwitch(area, on) {
    if (area.switch && this._hass) {
      this._hass.callService("switch", on ? "turn_on" : "turn_off", { entity_id: area.switch });
    } else {
      this._ui.on = on;
    }
  }
  _callSelect(entity, value, uiKey) {
    if (entity && this._hass) {
      this._hass.callService("select", "select_option", { entity_id: entity, option: value });
    } else if (uiKey) {
      this._ui[uiKey] = value;
    }
  }
  _callNumber(entity, value, uiKey) {
    if (entity && this._hass) {
      this._hass.callService("number", "set_value", { entity_id: entity, value });
    } else if (uiKey) {
      this._ui[uiKey] = value;
    }
  }

  /* ------------------------- Render ------------------------- */
  _render() {
    // A render error must NEVER bubble out of setConfig() - Lovelace turns that
    // into a dashboard-wide "Configuration error". Contain it, log it, and show
    // the actual message in-card so it can be diagnosed.
    try {
      this._renderImpl();
    } catch (err) {
      console.error("hue-music-sync-card: render failed", err);  // eslint-disable-line no-console
      try {
        // Built with DOM APIs (not innerHTML) so the error text can never be
        // interpreted as markup, whatever ends up in the message.
        this.shadowRoot.replaceChildren();
        const box = document.createElement("div");
        box.style.cssText =
          "padding:16px;font:13px system-ui,sans-serif;color:#ffb4b4;" +
          "background:#1a0f17;border-radius:12px;";
        box.appendChild(document.createTextNode(
          `Hue Synco card hit an error: ${String((err && err.message) || err)}`
        ));
        box.appendChild(document.createElement("br"));
        const hint = document.createElement("span");
        hint.style.color = "#9aa";
        hint.textContent = "Please report this message.";
        box.appendChild(hint);
        this.shadowRoot.appendChild(box);
      } catch (_) {
        /* shadowRoot not ready; nothing more we can do */
      }
    }
  }

  _renderImpl() {
    if (!this._config) return;
    // Don't tear the DOM down while an overlay page is open (it lives inside the
    // card node a render would replace). Each closes with its own _render().
    if (this._drum) return;
    const m = this._model();
    const accent = m.accent;
    const pal = m.colour.selected;

    // kick off album-art extraction if needed, and snapshot playback timing so the
    // visualizer loop can run the beat grid locked to the song (position + bpm).
    this._maybeExtractAlbum(m);
    this._play = {
      on: m.on,
      playing: m.now.playing,
      position: m.now.position || 0,
      updatedAt: m.now.updatedAt || Date.now(),
      bpm: m.bpm > 0 ? m.bpm : 122,
      beatAnchor: m.beatAnchor,
    };

    const card = document.createElement("div");
    card.className = "hue-card";
    this._cardNode = card;
    // Album/palette hue through the whole card (extends the hero wash past the
    // divider into the controls), over the default dark gradient.
    card.style.background = this._albumTintBackground(
      pal.colors, "var(--hue-card)", "#121120"
    );
    card.style.boxShadow = m.on
      ? `0 30px 80px -28px ${accent}77, 0 0 0 1px var(--hue-line)`
      : "0 0 0 1px var(--hue-line)";

    /* hero */
    const hero = document.createElement("div");
    hero.className = "hue-hero";

    // Blurred album art sits under the colour wash, making the whole hero
    // carry the current song. Applied via _applyArt once the URL has loaded.
    const heroArt = document.createElement("div");
    heroArt.className = "hue-hero-art";
    this._heroArtNode = heroArt;
    hero.appendChild(heroArt);

    const wash = document.createElement("div");
    wash.className = "hue-hero-wash";
    const wc = pal.colors;
    wash.style.background =
      `radial-gradient(80% 120% at 20% 10%, ${wc[0]}cc, transparent 55%),` +
      `radial-gradient(70% 110% at 90% 20%, ${wc[wc.length - 1]}bb, transparent 55%),` +
      `radial-gradient(90% 130% at 60% 100%, ${(wc[1] || wc[0])}99, transparent 60%)`;
    this._washNode = wash;
    hero.appendChild(wash);

    const heroBars = document.createElement("div");
    heroBars.className = "hue-hero-bars";
    const bars = document.createElement("div");
    bars.className = "hue-bars";
    this._barNodes = [];
    for (let i = 0; i < this._viz.count; i++) {
      const b = document.createElement("div");
      b.className = "hue-bar";
      bars.appendChild(b);
      this._barNodes.push(b);
    }
    this._barColors = pal.colors;
    this._barDim = (m.on && m.now.playing) ? 0.9 : 0.25;
    heroBars.appendChild(bars);
    hero.appendChild(heroBars);

    const heroTop = document.createElement("div");
    heroTop.className = "hue-hero-top";

    const pill = document.createElement("div");
    pill.className = "hue-pill";
    pill.style.boxShadow = m.on ? `inset 0 0 0 1px ${accent}66` : "";
    const pillDot = document.createElement("span");
    pillDot.className = "hue-pill-dot";
    // Surface the audio source: amber-warn when on the generic metadata
    // fallback (no real audio), so a dead tap is obvious at a glance.
    const SRC_LABEL = {
      "live-tap": "Live audio", snapcast: "Live (snapcast)",
      "track-map": "Track map", metadata: "Metadata only", idle: "Starting",
    };
    const onFallback = m.on && m.audioSource === "metadata";
    const pillColor = !m.on ? "#6b7088" : onFallback ? "#ffb24d" : accent;
    pillDot.style.background = pillColor;
    pillDot.style.boxShadow = m.on ? `0 0 8px ${pillColor}` : "none";
    pill.appendChild(pillDot);
    const label = m.on ? (SRC_LABEL[m.audioSource] || "Streaming") : "Idle";
    pill.appendChild(document.createTextNode(label));

    const pills = document.createElement("div");
    pills.className = "hue-hero-pills";
    pills.appendChild(pill);
    heroTop.appendChild(pills);

    heroTop.appendChild(this._power(m, accent));
    hero.appendChild(heroTop);

    // The cover is a tall tile on the left spanning the title AND transport
    // rows; the title/artist and the transport controls stack in a column to its
    // right, so the artwork gets the extra vertical room.
    const heroNow = document.createElement("div");
    heroNow.className = "hue-hero-now";
    heroNow.appendChild(this._cover(96, 18, m.now.art));

    const right = document.createElement("div");
    right.className = "hue-now-right";

    const topRow = document.createElement("div");
    topRow.className = "hue-now-toprow";
    const meta = document.createElement("div");
    meta.className = "hue-now-meta";
    const track = document.createElement("div");
    track.className = "hue-now-track";
    const trackInner = document.createElement("span");
    trackInner.className = "hue-now-track-inner";
    trackInner.textContent = m.now.track;
    track.appendChild(trackInner);
    this._marqueeNodes = [track, trackInner];
    const artist = document.createElement("div");
    artist.className = "hue-now-artist";
    artist.textContent = m.now.artist;
    meta.appendChild(track);
    meta.appendChild(artist);
    topRow.appendChild(meta);

    const brightMini = document.createElement("div");
    brightMini.className = "hue-bright-mini";
    brightMini.innerHTML = `<span class="hue-bright-mini-icon">\u2600</span><span>${Math.round(m.brightness.value)}%</span>`;
    topRow.appendChild(brightMini);
    right.appendChild(topRow);

    // Transport row: sits at the bottom of the right column (aligned with the
    // cover's bottom edge) so the artwork, title and controls form one compact
    // block. The buttons flex to fill the band; the time keeps to the right.
    if (m.now.player) {
      const tr = document.createElement("div");
      tr.className = "hue-transport";
      const svc = (service) => {
        if (this._hass) {
          this._hass.callService("media_player", service, { entity_id: m.now.player });
        }
      };
      // Inline SVG icons (fill: currentColor) rather than the Unicode media
      // glyphs: browsers render those as coloured emoji (a yellow/blue play
      // symbol that ignores CSS color); the icons must follow the theme.
      const ICONS = {
        prev: "M6 5h2v14H6zM20 5v14l-11-7z",
        play: "M8 5l12 7-12 7z",
        pause: "M7 5h4v14H7zM13 5h4v14h-4z",
        next: "M16 5h2v14h-2zM4 5l11 7-11 7z",
      };
      const mkBtn = (icon, service, label, main) => {
        const b = document.createElement("button");
        b.className = "hue-tr-btn" + (main ? " main" : "");
        const size = main ? 22 : 18;
        b.innerHTML =
          `<svg viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true">` +
          `<path d="${ICONS[icon]}" fill="currentColor"/></svg>`;
        b.setAttribute("aria-label", label);
        b.addEventListener("click", () => svc(service));
        return b;
      };
      tr.appendChild(mkBtn("prev", "media_previous_track", "Previous track"));
      tr.appendChild(
        mkBtn(m.now.playing ? "pause" : "play", "media_play_pause", "Play / pause", true)
      );
      tr.appendChild(mkBtn("next", "media_next_track", "Next track"));
      const time = document.createElement("div");
      time.className = "hue-tr-time";
      this._trTime = time;
      this._trDur = m.now.duration;
      tr.appendChild(time);
      right.appendChild(tr);
    } else {
      this._trTime = null;
    }

    heroNow.appendChild(right);
    hero.appendChild(heroNow);

    // Song-structure timeline (energy silhouette + playhead), filled from the
    // live meta feed once the track map is known.
    const tl = document.createElement("div");
    tl.className = "hue-tl";
    const tlMarker = document.createElement("div");
    tlMarker.className = "hue-tl-marker";
    tl.appendChild(tlMarker);
    this._tlNode = tl;
    this._tlMarker = tlMarker;
    this._tlSecs = null;
    this._tlSig = "";
    hero.appendChild(tl);
    card.appendChild(hero);

    /* body */
    const body = document.createElement("div");
    body.className = "hue-amb-body";

    // Room mirror: the actual lamps, live, laid out by their real positions.
    const stage = document.createElement("div");
    stage.className = "hue-stage";
    const tag = document.createElement("div");
    tag.className = "hue-stage-tag";
    const tagDot = document.createElement("span");
    tagDot.className = "hue-stage-tag-dot";
    tag.appendChild(tagDot);
    tag.appendChild(document.createTextNode("Live"));
    stage.appendChild(tag);
    const legend = document.createElement("div");
    legend.className = "hue-stage-legend";
    stage.appendChild(legend);
    this._stageNode = stage;
    this._stageLegend = legend;
    this._stageDots = null;
    this._stageSig = "";
    this._stageRoles = "";
    body.appendChild(stage);

    this._accent = accent;

    // Area + player selectors side by side, each under a section title like
    // the Intensity/Effect/Colour fields below them.
    const areaField = document.createElement("div");
    areaField.className = "hue-field";
    areaField.appendChild(this._label("Area", null));
    areaField.appendChild(this._areaSelect(accent));
    const playerDd = this._playerSelect(accent);
    if (playerDd) {
      const playerField = document.createElement("div");
      playerField.className = "hue-field";
      playerField.appendChild(this._label("Player", null));
      playerField.appendChild(playerDd);
      const selects = document.createElement("div");
      selects.className = "hue-grid2 tight hue-selects";
      selects.append(areaField, playerField);
      body.appendChild(selects);
    } else {
      body.appendChild(areaField);
    }

    const intensityHint = m.intensity.autoMode
      ? `Auto · ${titleize(m.intensity.autoMode)}` : null;
    const intensityField = this._segField(
      "Intensity", m.intensity.options, m.intensity.value, accent, (v) => {
        this._callSelect(m.intensity.entity, v, "intensity");
        this._render();
      }, true, intensityHint);
    // With Auto selected, offer the enabled-rungs checklist: the picker only
    // climbs into a rung that is ticked (Intense/Extreme stay off by default).
    if (m.intensity.value === "auto") {
      intensityField.appendChild(this._autoRangeDropdown(m, accent));
    }
    body.appendChild(intensityField);
    body.appendChild(this._advancedSection(m, accent));
    body.appendChild(
      this._segField("Effect", m.effect.options, m.effect.value, accent, (v) => {
        this._callSelect(m.effect.entity, v, "effect");
        this._render();
      })
    );

    // colour
    const colourField = document.createElement("div");
    colourField.className = "hue-field";
    colourField.appendChild(this._label("Colour", pal.name));
    colourField.appendChild(
      this._dots(m.colour.options, m.colour.value, (v) => {
        this._callSelect(m.colour.entity, v, "colour");
        this._render();
      })
    );
    body.appendChild(colourField);

    // brightness + timing grid
    const grid = document.createElement("div");
    grid.className = "hue-grid2 tight";

    const brightField = document.createElement("div");
    brightField.className = "hue-field";
    brightField.appendChild(this._label("Brightness"));
    brightField.appendChild(this._slider(m, accent));
    grid.appendChild(brightField);

    const timingField = document.createElement("div");
    timingField.className = "hue-field";
    timingField.appendChild(this._label("Timing"));
    timingField.appendChild(this._timing(m, accent));
    grid.appendChild(timingField);

    body.appendChild(grid);
    card.appendChild(body);

    this.shadowRoot.innerHTML = `<style>${CARD_CSS}</style>`;
    this.shadowRoot.appendChild(card);

    // Apply the artwork last (nodes for this render are in place). Synchronous
    // for an already-validated URL, so there is no flicker on re-renders.
    this._applyArt(m.now.art);

    // (Re)connect the live feed for the active area and repaint the stage /
    // timeline from the latest meta (the DOM nodes are fresh this render).
    this._ensureLiveSub();
    if (this._liveMeta) {
      this._syncStage(this._liveMeta);
      this._syncTimeline(this._liveMeta);
    }
    this._setupMarquee();
  }

  _setupMarquee() {
    const mq = this._marqueeNodes;
    if (!mq) return;
    const [outer, inner] = mq;
    // Measure after layout: long titles scroll gently back and forth.
    requestAnimationFrame(() => {
      const overflow = inner.scrollWidth - outer.clientWidth;
      if (overflow > 8) {
        inner.style.setProperty("--mq", `-${overflow + 12}px`);
        inner.classList.add("scroll");
      }
    });
  }

  /* -- room mirror (live lamp stage) -- */
  _syncStage(meta) {
    const stage = this._stageNode;
    if (!stage) return;
    const positions = meta && meta.positions;
    if (!positions || !Object.keys(positions).length) {
      stage.classList.remove("live");
      return;
    }
    const sig = JSON.stringify(positions);
    if (sig === this._stageSig) {
      stage.classList.add("live");
      return;
    }
    this._stageSig = sig;
    // Rebuild the dots (lamp set / layout changed).
    if (this._stageDots) {
      for (const node of Object.values(this._stageDots)) node.remove();
    }
    this._stageDots = {};
    // Order the lamps left-to-right by their x position: that is exactly the
    // engine's spectral rank (left = low frequencies, right = high), so a lamp's
    // horizontal place on the map is the slice of the spectrum it reacts to.
    const entries = Object.entries(positions).sort((a, b) => a[1][0] - b[1][0]);
    const n = entries.length;
    const zs = entries.map((e) => e[1][2]);
    const zMin = Math.min(...zs);
    const zSpan = Math.max(...zs) - zMin;
    entries.forEach(([cid, pos], i) => {
      const dot = document.createElement("div");
      dot.className = "hue-stage-dot";
      const fx = n > 1 ? i / (n - 1) : 0.5; // spectral position 0..1
      // Spread evenly across the width so the lamps never clump in a corner.
      const left = 9 + fx * 82;
      // Vertical: real height when the lamps differ in height, otherwise a
      // gentle centred arc so a same-height row still reads as a room.
      const top = zSpan > 0.05
        ? 80 - ((pos[2] - zMin) / zSpan) * 52
        : 50 - Math.sin(fx * Math.PI) * 14;
      dot.style.left = `${left.toFixed(1)}%`;
      dot.style.top = `${top.toFixed(1)}%`;
      dot._fx = fx;
      this._stageDots[cid] = dot;
      stage.appendChild(dot);
    });
    this._buildStageLegend();
    this._stageRoles = "";
    stage.classList.add("live");
  }

  _buildStageLegend() {
    // The room map now shows the full audio spectrum the lamps cover, low on the
    // left to high on the right - so the legend is a Low -> High spectrum bar,
    // not the three named roles. It adapts to every instrument in the music.
    const legend = this._stageLegend;
    if (!legend) return;
    const bar = document.createElement("i");
    bar.className = "hue-stage-spectrum";
    const stops = [0, 0.25, 0.5, 0.75, 1].map((t) => spectrumColor(t));
    bar.style.background = `linear-gradient(90deg, ${stops.join(", ")})`;
    const lo = document.createElement("span");
    lo.textContent = "Low";
    const hi = document.createElement("span");
    hi.textContent = "High";
    legend.replaceChildren(lo, bar, hi);
  }

  _applyStageLive(live) {
    if (!this._stageDots) return;
    const lights = (live && live.lights) || {};
    const roles = (live && live.roles) || {};
    for (const [cid, dot] of Object.entries(this._stageDots)) {
      const hex = lights[cid];
      // The ring tints the lamp by its place in the spectrum (its frequency
      // slice), so the map reads as low -> high across the room.
      const ring = spectrumColor(dot._fx || 0);
      if (hex) {
        // Glow scales with the lamp's actual brightness (max RGB channel).
        const v = Math.max(
          parseInt(hex.slice(1, 3), 16),
          parseInt(hex.slice(3, 5), 16),
          parseInt(hex.slice(5, 7), 16)
        ) / 255;
        dot.style.background = hex;
        dot.style.boxShadow =
          `0 0 ${(4 + v * 18).toFixed(0)}px ${hex}, 0 0 0 2px ${ring}cc`;
      } else {
        dot.style.boxShadow = `0 0 0 2px ${ring}55`;
      }
      // Pop the dot when its instrument role rotates (the roles still trade
      // seats around the room), even though the ring is spectrum-tinted.
      const role = roles[cid];
      if (dot._role !== role) {
        if (dot._role != null) {
          dot.classList.remove("swap");
          void dot.offsetWidth; // restart the swap animation
          dot.classList.add("swap");
        }
        dot._role = role;
      }
    }
  }

  /* -- song-structure timeline -- */
  _syncTimeline(meta) {
    const tl = this._tlNode;
    if (!tl) return;
    const sections = meta && meta.sections;
    const duration = meta && meta.duration;
    if (!sections || !sections.length || !duration) {
      tl.classList.remove("live");
      return;
    }
    const sig = JSON.stringify(sections) + "|" + duration + "|" + this._accent;
    if (sig !== this._tlSig) {
      this._tlSig = sig;
      if (this._tlSecs) for (const n of this._tlSecs) n.node.remove();
      this._tlSecs = sections.map(([start, end, energy]) => {
        const node = document.createElement("div");
        node.className = "hue-tl-sec";
        node.style.left = `${((start / duration) * 100).toFixed(2)}%`;
        node.style.width = `${(((end - start) / duration) * 100 - 0.5).toFixed(2)}%`;
        node.style.height = `${(22 + energy * 78).toFixed(0)}%`;
        node.style.background = `${this._accent}${energy > 0.6 ? "cc" : energy > 0.3 ? "77" : "44"}`;
        this._tlNode.insertBefore(node, this._tlMarker);
        return { node, start, end, energy };
      });
    }
    tl.classList.add("live");
  }

  _applyTimelineLive() {
    const meta = this._liveMeta;
    if (!meta || !this._tlSecs || !this._tlMarker || !meta.duration) return;
    let pos = meta.position || 0;
    if (meta.playing) pos += (Date.now() - meta.at) / 1000;
    pos = Math.max(0, Math.min(meta.duration, pos));
    this._tlMarker.style.left = `${((pos / meta.duration) * 100).toFixed(2)}%`;
    let current = null;
    for (const s of this._tlSecs) {
      if (pos >= s.start && pos < s.end) current = s;
      s.node.classList.toggle("past", s.end <= pos);
    }
    // Section change into a clearly louder one: the drop landed - bloom.
    if (current && current !== this._curSec) {
      if (this._curSec && current.energy > this._curSec.energy + 0.15) {
        this._bloom = 1;
      }
      this._curSec = current;
    }
    // Drop anticipation: the next clearly-louder section "arms" as it nears.
    for (const s of this._tlSecs) {
      const arming =
        current !== null &&
        s.start > pos &&
        s.start - pos < 10 &&
        s.energy > current.energy + 0.15;
      s.node.classList.toggle("arming", arming);
    }
  }

  /* -- album-art application (preload-validated) -- */
  _applyArt(url) {
    if (!url) {
      // No artwork: the cover keeps its placeholder gradient and the hero
      // background stays hidden.
      this._artWanted = null;
      return;
    }
    const apply = (u) => {
      const css = `url("${u}")`;
      if (this._coverArtNode) this._coverArtNode.style.backgroundImage = css;
      if (this._heroArtNode) {
        this._heroArtNode.style.backgroundImage = css;
        this._heroArtNode.classList.add("show");
      }
    };
    if (url === this._artGoodUrl) {
      apply(url);
      return;
    }
    // Validate before applying: a stale tokenised proxy URL that 404s would
    // otherwise wipe the placeholder and leave an empty dark tile.
    this._artWanted = url;
    const img = new Image();
    img.onload = () => {
      if (this._artWanted !== url) return; // track moved on while loading
      this._artGoodUrl = url;
      apply(url);
    };
    img.onerror = () => {}; // keep placeholder / previous art
    img.src = url;
  }

  /* -- primitives -- */
  _power(m, accent) {
    const btn = document.createElement("button");
    btn.className = "hue-power" + (m.on ? " on" : "");
    btn.setAttribute("aria-label", "Toggle sync");
    if (m.on) {
      btn.style.background = accent;
      btn.style.boxShadow = `0 0 24px ${accent}aa, inset 0 0 0 1px #fff3`;
    }
    const knob = document.createElement("span");
    knob.className = "hue-power-knob";
    if (m.on) knob.style.boxShadow = `0 2px 8px ${accent}`;
    btn.appendChild(knob);
    btn.addEventListener("click", () => {
      this._callSwitch(m.area, !m.on);
      this._render();
    });
    return btn;
  }

  _cover(size, radius, _art) {
    const wrap = document.createElement("div");
    wrap.className = "hue-cover";
    wrap.style.width = size + "px";
    wrap.style.height = size + "px";
    wrap.style.borderRadius = radius + "px";
    const a = document.createElement("div");
    a.className = "hue-cover-art";
    a.style.borderRadius = radius + "px";
    // The art URL is applied by _applyArt after preload-validation; until then
    // (or when there is no art) the CSS placeholder gradient shows.
    this._coverArtNode = a;
    const gloss = document.createElement("div");
    gloss.className = "hue-cover-gloss";
    gloss.style.borderRadius = radius + "px";
    this._glossNode = gloss;
    wrap.appendChild(a);
    wrap.appendChild(gloss);
    return wrap;
  }

  // Is the given area's sync currently running? (unknown in demo / no switch)
  _areaOn(a) {
    if (!a || !a.switch || !this._hass) return false;
    const e = this._hass.states[a.switch];
    return !!e && e.state === "on";
  }

  _areaSelect(accent) {
    const dd = document.createElement("div");
    dd.className = "hue-area-dd" + (this._areaMenuOpen ? " open" : "");
    const multi = this._areas.length > 1;
    const sel = this._areas[this._areaIndex] || {};
    const selOn = this._areaOn(sel);

    // Trigger: shows the area being viewed, and whether it's the one syncing.
    const trigger = document.createElement("button");
    trigger.className = "hue-area-trigger" + (multi ? "" : " static");
    const dot = document.createElement("span");
    dot.className = "hue-area-dot";
    dot.style.background = selOn ? accent : "#5a5f78";
    dot.style.boxShadow = selOn ? `0 0 8px ${accent}` : "none";
    const name = document.createElement("span");
    name.className = "hue-area-name";
    name.textContent = sel.name || "Area";
    trigger.append(dot, name);
    if (selOn) {
      const badge = document.createElement("span");
      badge.className = "hue-area-badge";
      badge.textContent = "On";
      badge.style.color = accent;
      trigger.appendChild(badge);
    }
    if (multi) {
      const caret = document.createElement("span");
      caret.className = "hue-area-caret";
      caret.textContent = "▼";
      trigger.appendChild(caret);
      trigger.setAttribute("aria-haspopup", "listbox");
      trigger.setAttribute("aria-expanded", this._areaMenuOpen ? "true" : "false");
      trigger.addEventListener("click", () => {
        this._areaMenuOpen = !this._areaMenuOpen;
        this._autoMenuOpen = false;
        this._render();
      });
    }
    dd.appendChild(trigger);

    if (multi && this._areaMenuOpen) {
      const menu = document.createElement("div");
      menu.className = "hue-area-menu";
      menu.setAttribute("role", "listbox");
      this._areas.forEach((a, i) => {
        const isSel = i === this._areaIndex;
        const on = this._areaOn(a);
        const row = document.createElement("button");
        row.className = "hue-area-row" + (isSel ? " sel" : "");
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", isSel ? "true" : "false");
        const rdot = document.createElement("span");
        rdot.className = "hue-area-dot";
        rdot.style.background = on ? accent : "#5a5f78";
        rdot.style.boxShadow = on ? `0 0 8px ${accent}` : "none";
        const rname = document.createElement("span");
        rname.className = "hue-area-row-name";
        rname.textContent = a.name;
        row.append(rdot, rname);
        if (on) {
          const rb = document.createElement("span");
          rb.className = "hue-area-badge";
          rb.textContent = "On";
          rb.style.color = accent;
          row.appendChild(rb);
        }
        const check = document.createElement("span");
        check.className = "hue-area-check";
        check.textContent = isSel ? "✓" : "";
        check.style.color = accent;
        row.appendChild(check);
        row.addEventListener("click", () => {
          this._areaIndex = i;
          this._areaMenuOpen = false;
          this._areaAutoDone = true; // a manual pick pins the view
          this._render();
        });
        menu.appendChild(row);
      });
      dd.appendChild(menu);
      this._placeAreaMenu(trigger, menu);
    }
    return dd;
  }

  // Anchor the fixed-position area menu to its trigger, flipping above when
  // there's more room there, clamping to the viewport, and capping height so a
  // long area list scrolls inside the menu instead of being clipped by the
  // card's overflow:hidden. Runs after attach (the menu needs layout to size).
  _placeAreaMenu(trigger, menu) {
    requestAnimationFrame(() => {
      if (!menu.isConnected) return;
      const r = trigger.getBoundingClientRect();
      const vh = window.innerHeight;
      const vw = window.innerWidth;
      const gap = 6;      // trigger-to-menu spacing
      const margin = 8;   // minimum distance from the viewport edge
      const below = vh - r.bottom - gap - margin;
      const above = r.top - gap - margin;
      const desired = Math.min(380, menu.scrollHeight + 2);
      let maxH;
      let top;
      if (below >= Math.min(desired, 220) || below >= above) {
        maxH = Math.max(120, Math.min(desired, below));
        top = r.bottom + gap;
      } else {
        maxH = Math.max(120, Math.min(desired, above));
        top = r.top - gap - maxH;
      }
      menu.style.maxHeight = `${maxH}px`;
      menu.style.top = `${Math.max(margin, top)}px`;
      const w = menu.offsetWidth || 210;
      menu.style.left = `${Math.min(Math.max(margin, r.left), vw - w - margin)}px`;
      menu.style.visibility = "visible";
    });
  }

  _label(text, value) {
    const l = document.createElement("div");
    l.className = "hue-label";
    l.appendChild(document.createTextNode(text));
    if (value != null) {
      const v = document.createElement("span");
      v.className = "hue-label-val";
      v.textContent = value;
      l.appendChild(v);
    }
    return l;
  }

  _segField(label, options, value, accent, onChange, previews = false, valueOverride = null) {
    const field = document.createElement("div");
    field.className = "hue-field";
    const sel = options.find((o) => o.value === value);
    field.appendChild(this._label(label, valueOverride != null ? valueOverride : (sel ? sel.label : "")));
    field.appendChild(this._segmented(options, value, accent, onChange, previews));
    return field;
  }

  _segmented(options, value, accent, onChange, previews = false) {
    const seg = document.createElement("div");
    seg.className = "hue-seg";
    options.forEach((o) => {
      const on = o.value === value;
      const b = document.createElement("button");
      b.className = "hue-seg-btn" + (on ? " on" : "");
      if (on) b.style.boxShadow = `inset 0 0 0 1px ${accent}55, 0 0 18px ${accent}33`;
      if (on) {
        const glow = document.createElement("span");
        glow.className = "hue-seg-glow";
        glow.style.background = accent;
        b.appendChild(glow);
      }
      const lab = document.createElement("span");
      lab.className = "hue-seg-label";
      lab.textContent = o.label;
      b.appendChild(lab);
      if (previews) {
        // A 1-second looping micro-preview of the mode's character, so users
        // pick an intensity by feel rather than by name.
        const anim = document.createElement("span");
        anim.className = `hue-seg-anim m-${String(o.value).toLowerCase()}`;
        anim.style.color = on ? accent : "#8d89a8";
        b.appendChild(anim);
      }
      b.addEventListener("click", () => onChange(o.value));
      seg.appendChild(b);
    });
    return seg;
  }

  // The Auto enabled-rungs control: a compact dropdown of checkable rungs,
  // shown only when Intensity is Auto. Reuses the area/player popover machinery
  // (fixed-position menu, outside-click close, viewport clamping) so it behaves
  // and looks the same on the mobile and tablet cards. Ticking Intense/Extreme
  // lets the picker climb there on a big enough moment; the last ticked rung
  // can't be removed (Auto always needs somewhere to sit).
  _autoRangeDropdown(m, accent) {
    const RUNGS = ["subtle", "medium", "high", "intense", "extreme"];
    const selected = new Set(
      (m.intensity.autoLevels || []).map((s) => String(s).toLowerCase())
    );
    const live = m.intensity.autoMode
      ? String(m.intensity.autoMode).toLowerCase() : null;

    const dd = document.createElement("div");
    dd.className = "hue-area-dd hue-autorange" + (this._autoMenuOpen ? " open" : "");

    // Trigger: summarises the enabled rungs; opens the checklist.
    const trigger = document.createElement("button");
    trigger.className = "hue-area-trigger";
    const name = document.createElement("span");
    name.className = "hue-area-name";
    const chosen = RUNGS.filter((r) => selected.has(r)).map(titleize);
    name.textContent = "Auto range: " + (chosen.join(" · ") || "—");
    const caret = document.createElement("span");
    caret.className = "hue-area-caret";
    caret.textContent = "▼";
    trigger.append(name, caret);
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", this._autoMenuOpen ? "true" : "false");
    trigger.addEventListener("click", () => {
      this._autoMenuOpen = !this._autoMenuOpen;
      this._areaMenuOpen = false;
      this._playerMenuOpen = false;
      this._render();
    });
    dd.appendChild(trigger);

    if (this._autoMenuOpen) {
      const menu = document.createElement("div");
      menu.className = "hue-area-menu";
      menu.setAttribute("role", "listbox");
      menu.setAttribute("aria-multiselectable", "true");
      RUNGS.forEach((rung) => {
        const on = selected.has(rung);
        const isLive = on && rung === live;
        const row = document.createElement("button");
        row.className = "hue-area-row" + (on ? " sel" : "");
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", on ? "true" : "false");
        const rname = document.createElement("span");
        rname.className = "hue-area-row-name";
        rname.textContent = titleize(rung);
        row.appendChild(rname);
        if (isLive) {
          // Marks the rung Auto is sitting on right now.
          const badge = document.createElement("span");
          badge.className = "hue-area-badge";
          badge.textContent = "Now";
          badge.style.color = accent;
          row.appendChild(badge);
        }
        const check = document.createElement("span");
        check.className = "hue-area-check";
        check.textContent = on ? "✓" : "";
        check.style.color = accent;
        row.appendChild(check);
        row.addEventListener("click", (e) => {
          e.stopPropagation();  // toggling keeps the menu open
          const next = new Set(selected);
          if (next.has(rung)) {
            if (next.size <= 1) return;  // keep at least one rung enabled
            next.delete(rung);
          } else {
            next.add(rung);
          }
          const levels = RUNGS.filter((r) => next.has(r));  // canonical order
          this._callAutoLevels(m.area, levels);
          this._render();
        });
        menu.appendChild(row);
      });
      dd.appendChild(menu);
      this._placeAreaMenu(trigger, menu);
    }
    return dd;
  }

  _callAutoLevels(area, levels) {
    // Optimistic: reflect the tick immediately (dirty until the integration
    // echoes it back), then persist via the service (which live-applies to a
    // running session). Demo mode just updates the UI.
    this._ui.autoLevels = levels;
    this._autoLevelsDirty = true;
    if (area && area.switch && this._hass) {
      this._hass.callService("hue_music_sync", "set_options", {
        entity_id: area.switch, auto_levels: levels,
      });
    }
  }

  _callAutoTiming(area, on) {
    // Optimistic like the rungs: reflect the toggle at once, persist via service.
    this._ui.autoTiming = on;
    this._autoTimingDirty = true;
    if (area && area.switch && this._hass) {
      this._hass.callService("hue_music_sync", "set_options", {
        entity_id: area.switch, auto_timing: on,
      });
    }
  }

  _callAdvanced(m, on) {
    // Optimistic toggle; persisted via set_options (also flips the Advanced
    // switch entity). Demo mode just updates the local UI.
    this._ui.advanced = on;
    this._advDirty = true;
    if (m.area && m.area.switch && this._hass) {
      this._hass.callService("hue_music_sync", "set_options", {
        entity_id: m.area.switch, advanced: on,
      });
    }
  }

  _sendTunables(m, tun) {
    // Send the FULL factor map (the integration replaces the stored dict).
    this._ui.tunables = tun;
    this._tunDirty = true;
    if (m.area && m.area.switch && this._hass) {
      this._hass.callService("hue_music_sync", "set_options", {
        entity_id: m.area.switch, tunables: tun,
      });
    }
  }

  _callTunable(m, key, factor) {
    this._sendTunables(m, Object.assign({}, m.tunables, { [key]: factor }));
  }

  _resetTunables(m) {
    const tun = {};
    for (const k of TUNABLE_KEYS) tun[k] = 1.0;
    this._sendTunables(m, tun);
  }

  // Card background tinted by the album/palette colours: soft glows from the top
  // corners that reach well down the card plus one rising from the bottom, over
  // the base gradient — so the current song's hue bleeds through the WHOLE card
  // (past the hero divider), not just the header band.
  _albumTintBackground(wc, baseFrom, baseTo) {
    const a = wc[0];
    const b = wc[wc.length - 1] || wc[0];
    const c = wc[1] || wc[0];
    return (
      `radial-gradient(115% 75% at 15% -8%, ${a}40 0%, transparent 60%),` +
      `radial-gradient(110% 70% at 90% 0%, ${b}34 0%, transparent 58%),` +
      `radial-gradient(130% 78% at 55% 114%, ${c}26 0%, transparent 62%),` +
      `linear-gradient(180deg, ${baseFrom} 0%, ${baseTo} 100%)`
    );
  }

  _dots(options, value, onChange) {
    const wrap = document.createElement("div");
    wrap.className = "hue-dots";
    options.forEach((o) => {
      const on = o.value === value;
      const b = document.createElement("button");
      b.className = "hue-dot" + (on ? " on" : "");
      b.title = o.name;
      b.style.background = gradFor(o.colors, 145);
      b.style.boxShadow = on
        ? `0 0 0 2px var(--hue-bg), 0 0 0 4px ${o.colors[0]}, 0 0 16px ${o.colors[0]}aa`
        : "none";
      if (o.album) {
        const ring = document.createElement("span");
        ring.className = "hue-dot-ring";
        b.appendChild(ring);
      }
      b.addEventListener("click", () => onChange(o.value));
      wrap.appendChild(b);
    });
    return wrap;
  }

  _slider(m, accent) {
    const { value, min, max } = m.brightness;
    const row = document.createElement("div");
    row.className = "hue-slider-row";
    const icon = document.createElement("span");
    icon.className = "hue-slider-icon";
    icon.textContent = "\u2600";
    row.appendChild(icon);

    const slider = document.createElement("div");
    slider.className = "hue-slider";
    const track = document.createElement("div");
    track.className = "hue-slider-track";
    const fill = document.createElement("div");
    fill.className = "hue-slider-fill";
    const knob = document.createElement("div");
    knob.className = "hue-slider-knob";
    slider.appendChild(track);
    slider.appendChild(fill);
    slider.appendChild(knob);

    const valEl = document.createElement("span");
    valEl.className = "hue-slider-val";
    const suf = document.createElement("span");
    suf.className = "hue-slider-suf";
    suf.textContent = "%";

    const paint = (v) => {
      const pct = ((v - min) / (max - min)) * 100;
      fill.style.width = pct + "%";
      fill.style.background = gradFor([accent + "88", accent]);
      knob.style.left = pct + "%";
      knob.style.background = accent;
      knob.style.boxShadow = `0 0 0 4px ${accent}33, 0 0 16px ${accent}`;
      valEl.textContent = Math.round(v);
      valEl.appendChild(suf);
    };
    paint(value);

    let current = value;
    const fromEvent = (clientX) => {
      const r = slider.getBoundingClientRect();
      let p = (clientX - r.left) / r.width;
      p = Math.max(0, Math.min(1, p));
      let v = min + p * (max - min);
      v = Math.round(v);
      v = Math.max(min, Math.min(max, v));
      current = v;
      paint(v);
    };
    const onMove = (e) => {
      if (!this._dragging) return;
      const x = e.clientX ?? (e.touches && e.touches[0] && e.touches[0].clientX);
      if (x != null) fromEvent(x);
    };
    const onUp = () => {
      if (!this._dragging) return;
      this._dragging = false;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      this._callNumber(m.brightness.entity, current, "brightness");
      this._render();
    };
    slider.addEventListener("pointerdown", (e) => {
      this._dragging = true;
      slider.setPointerCapture && slider.setPointerCapture(e.pointerId);
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      fromEvent(e.clientX);
    });

    row.appendChild(slider);
    row.appendChild(valEl);
    return row;
  }

  _advancedSection(m, accent) {
    const field = document.createElement("div");
    field.className = "hue-field hue-advanced";

    const head = document.createElement("button");
    head.type = "button";
    head.className = "hue-adv-toggle" + (m.advanced ? " on" : "");
    head.setAttribute("aria-pressed", m.advanced ? "true" : "false");
    head.textContent = "⚙ Advanced";
    if (m.advanced) head.style.background = accent;
    head.addEventListener("click", () => {
      this._callAdvanced(m, !m.advanced);
      this._render();
    });
    field.appendChild(head);

    if (m.advanced) {
      const knobs = document.createElement("div");
      knobs.className = "hue-tun-knobs";
      for (const def of TUNABLE_DEFS) knobs.appendChild(this._tunableSlider(m, accent, def));
      field.appendChild(knobs);
      const reset = document.createElement("button");
      reset.type = "button";
      reset.className = "hue-adv-reset";
      reset.textContent = "Reset to 100%";
      reset.addEventListener("click", () => {
        this._resetTunables(m);
        this._render();
      });
      field.appendChild(reset);
    }
    return field;
  }

  _tunableSlider(m, accent, def) {
    const factor = m.tunables && m.tunables[def.key] != null ? m.tunables[def.key] : 1.0;
    const min = 0;
    const max = 200;
    const row = document.createElement("div");
    row.className = "hue-slider-row hue-tun-row";
    const lab = document.createElement("span");
    lab.className = "hue-tun-label";
    lab.textContent = `${def.icon} ${def.label}`;
    row.appendChild(lab);

    const slider = document.createElement("div");
    slider.className = "hue-slider";
    const track = document.createElement("div");
    track.className = "hue-slider-track";
    const fill = document.createElement("div");
    fill.className = "hue-slider-fill";
    const knob = document.createElement("div");
    knob.className = "hue-slider-knob";
    slider.append(track, fill, knob);

    const valEl = document.createElement("span");
    valEl.className = "hue-slider-val";
    const suf = document.createElement("span");
    suf.className = "hue-slider-suf";
    suf.textContent = "%";

    const paint = (pct) => {
      const p = ((pct - min) / (max - min)) * 100;
      fill.style.width = p + "%";
      fill.style.background = gradFor([accent + "88", accent]);
      knob.style.left = p + "%";
      knob.style.background = accent;
      knob.style.boxShadow = `0 0 0 4px ${accent}33, 0 0 16px ${accent}`;
      valEl.textContent = Math.round(pct);
      valEl.appendChild(suf);
    };
    let current = Math.round(factor * 100);
    paint(current);

    const fromEvent = (clientX) => {
      const r = slider.getBoundingClientRect();
      const p = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
      current = Math.max(min, Math.min(max, Math.round(min + p * (max - min))));
      paint(current);
    };
    const onMove = (e) => {
      if (!this._dragging) return;
      const x = e.clientX ?? (e.touches && e.touches[0] && e.touches[0].clientX);
      if (x != null) fromEvent(x);
    };
    const onUp = () => {
      if (!this._dragging) return;
      this._dragging = false;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      this._callTunable(m, def.key, current / 100);
      this._render();
    };
    slider.addEventListener("pointerdown", (e) => {
      this._dragging = true;
      slider.setPointerCapture && slider.setPointerCapture(e.pointerId);
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      fromEvent(e.clientX);
    });

    row.append(slider, valEl);
    return row;
  }

  _timing(m, accent) {
    const { value, min, max, step, entity, auto, autoMs, locked } = m.timing;
    const clamp = (v) => Math.max(min, Math.min(max, v));
    const wrap = document.createElement("div");
    wrap.className = "hue-timing";

    // Auto toggle: calibrate the light delay per song instead of the manual
    // trim. When on, the steppers are disabled and the readout shows the live
    // calibrated value.
    const autoBtn = document.createElement("button");
    autoBtn.className = "hue-step hue-timing-auto" + (auto ? " on" : "");
    autoBtn.textContent = "A";
    autoBtn.title = auto
      ? "Auto timing on — the delay is calibrated per song"
      : "Auto timing — calibrate the light delay per song";
    autoBtn.setAttribute("aria-pressed", auto ? "true" : "false");
    if (auto) { autoBtn.style.color = "#fff"; autoBtn.style.background = accent; }
    autoBtn.addEventListener("click", () => {
      this._callAutoTiming(m.area, !auto);
      this._render();
    });

    const mk = (sym, delta, label) => {
      const b = document.createElement("button");
      b.className = "hue-step";
      b.textContent = sym;
      b.setAttribute("aria-label", label);
      if (auto) {
        b.disabled = true;
        b.style.opacity = "0.4";
      } else {
        b.addEventListener("click", () => {
          this._callNumber(entity, clamp(value + delta), "timing");
          this._render();
        });
      }
      return b;
    };

    const readout = document.createElement("div");
    readout.className = "hue-timing-readout";
    readout.style.boxShadow = `inset 0 0 0 1px ${accent}33`;
    const num = document.createElement("span");
    num.className = "hue-timing-num";
    const unit = document.createElement("span");
    unit.className = "hue-timing-unit";
    if (auto) {
      if (locked && autoMs != null) {
        num.style.color = accent;
        num.textContent = (autoMs > 0 ? "+" : "") + autoMs;
        unit.textContent = "ms";
      } else {
        // Still settling on this song.
        num.style.color = "var(--hue-dim)";
        num.textContent = "Auto";
        unit.textContent = "";
      }
    } else {
      num.style.color = value === 0 ? "var(--hue-dim)" : accent;
      num.textContent = (value > 0 ? "+" : "") + value;
      unit.textContent = "ms";
    }
    readout.appendChild(num);
    readout.appendChild(unit);

    wrap.appendChild(autoBtn);
    wrap.appendChild(mk("-", -step, "Earlier"));
    wrap.appendChild(readout);
    wrap.appendChild(mk("+", step, "Later"));
    // Play-the-beats: open the drum pad and drive the lights with your taps.
    const tap = document.createElement("button");
    tap.className = "hue-step";
    tap.textContent = "\ud83e\udd41"; // drum
    tap.title = "Beat Pads";
    tap.setAttribute("aria-label", "Open the beat pads to play the lights");
    tap.addEventListener("click", () => this._startDrums(m));
    wrap.appendChild(tap);
    return wrap;
  }

  /* -- player dropdown --
     Which player drives the lights. The integration auto-picks whatever is
     playing, which is ambiguous the moment two players are playing different
     songs, so let the user pin one. The list is the *active* Music Assistant
     players (short and relevant), plus whichever player is pinned or currently
     followed even if it is neither, so a non-MA player (a Subsonic radio on the
     same library) never disappears from its own menu. The pin is persisted by
     the integration (hue_music_sync.set_options), so it survives a restart;
     "Auto" clears it. Rendered as a dropdown next to the area selector, sharing
     the area dropdown's trigger/menu styling and fixed-position placement. */
  _playerSelect(accent) {
    const area = this._areas[this._areaIndex] || {};
    if (this._demo || !area.switch) return null;

    const data = this._players;
    // The switch publishes the player being followed right now: when that moves,
    // the cached list is out of date whatever its age, so refresh it at once.
    const swEnt = this._hass && this._hass.states[area.switch];
    const livePlayer = (swEnt && swEnt.attributes.source_player) || null;
    this._ensurePlayers(!!data && (data.following || null) !== livePlayer);

    const players = (data && data.players) || [];
    const selected = (data && data.selected) || null;
    const following = (data && data.following) || null;
    const rowFor = (id) => players.find((p) => p.entity_id === id) || null;
    const nameOf = (id) => {
      const row = rowFor(id);
      return row ? row.name : id;
    };

    const dd = document.createElement("div");
    dd.className = "hue-area-dd" + (this._playerMenuOpen ? " open" : "");

    const trigger = document.createElement("button");
    trigger.className = "hue-area-trigger";
    const dot = document.createElement("span");
    dot.className = "hue-area-dot";
    // Amber when a pinned player is not the one being followed (typically the
    // pinned one is not playing); otherwise "why are the lights dead?" is
    // invisible.
    const on = !!swEnt && swEnt.state === "on";
    const stale = !!selected && !!following && selected !== following;
    const idle = !!selected && !following && on;
    const colour = stale || idle ? "#ffb24d" : following ? accent : "#5a5f78";
    dot.style.background = colour;
    dot.style.boxShadow = following && !stale ? `0 0 8px ${colour}` : "none";
    const name = document.createElement("span");
    name.className = "hue-area-name";
    name.textContent = selected ? nameOf(selected) : "Auto";
    const caret = document.createElement("span");
    caret.className = "hue-area-caret";
    caret.textContent = "▼";
    trigger.append(dot, name, caret);
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", this._playerMenuOpen ? "true" : "false");
    trigger.title = selected
      ? `Lights follow ${nameOf(selected)}` +
        (stale ? ` (currently playing: ${nameOf(following)})` : "")
      : following
        ? `Auto: currently following ${nameOf(following)}`
        : "Auto: follows whichever player is playing";
    trigger.addEventListener("click", () => {
      this._playerMenuOpen = !this._playerMenuOpen;
      this._areaMenuOpen = false;
      this._autoMenuOpen = false;
      if (this._playerMenuOpen) this._ensurePlayers(true); // fresh list at open
      this._render();
    });
    dd.appendChild(trigger);

    if (this._playerMenuOpen) {
      const menu = document.createElement("div");
      menu.className = "hue-area-menu";
      menu.setAttribute("role", "listbox");
      const rows = [{
        entity_id: null,
        name: "Auto",
        sub: "Follow whichever player is playing",
        state: null,
      }].concat(players.map((x) => ({
        entity_id: x.entity_id,
        name: x.name,
        sub: x.title
          ? (x.artist ? `${x.title} · ${x.artist}` : x.title)
          : titleize(x.state),
        state: x.state,
      })));
      for (const r of rows) {
        const row = document.createElement("button");
        const isSel = selected === r.entity_id;
        row.className = "hue-area-row" + (isSel ? " sel" : "");
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", isSel ? "true" : "false");
        const rdot = document.createElement("span");
        rdot.className = "hue-area-dot";
        rdot.style.background =
          r.state === null ? (accent || "#7b5cff")
            : r.state === "playing" ? "#5ee08a"
              : r.state === "unavailable" ? "#ff6b7a" : "#6b7088";
        const col = document.createElement("span");
        col.className = "hue-area-row-col";
        const rname = document.createElement("span");
        rname.className = "hue-area-row-name";
        rname.textContent = r.name;
        col.appendChild(rname);
        if (r.sub) {
          const rsub = document.createElement("span");
          rsub.className = "hue-area-row-sub";
          rsub.textContent = r.sub;
          col.appendChild(rsub);
        }
        const check = document.createElement("span");
        check.className = "hue-area-check";
        check.textContent = isSel ? "✓" : "";
        check.style.color = accent;
        row.append(rdot, col, check);
        row.addEventListener("click", () => this._choosePlayer(r.entity_id));
        menu.appendChild(row);
      }
      if (!players.length) {
        const empty = document.createElement("div");
        empty.className = "hue-area-row-sub";
        empty.style.padding = "6px 10px";
        empty.textContent = "No Music Assistant player is playing right now.";
        menu.appendChild(empty);
      }
      dd.appendChild(menu);
      this._placeAreaMenu(trigger, menu);
    }
    return dd;
  }

  _ensurePlayers(force) {
    const area = this._areas[this._areaIndex] || {};
    const sw = area.switch;
    const conn = this._hass && this._hass.connection;
    if (!conn || !sw || this._demo || this._playersReq) return;
    const now = Date.now();
    if (!force && this._playersFor === sw && now - this._playersAt < 15000) return;
    this._playersReq = true;
    this._playersFor = sw;
    this._playersAt = now;
    try {
      conn
        .sendMessagePromise({ type: "hue_music_sync/players", entity_id: sw })
        .then((res) => {
          this._playersReq = false;
          this._players = res;
          this._render();
        })
        .catch(() => {
          // Older integration, or the switch isn't registered yet: the trigger
          // just keeps showing what we last knew (or "Auto").
          this._playersReq = false;
        });
    } catch (_) {
      this._playersReq = false; // connection went away
    }
  }

  _choosePlayer(entityId) {
    const area = this._areas[this._areaIndex] || {};
    if (this._hass && area.switch && !this._demo) {
      // An empty media_player clears the pin: back to auto-picking.
      this._hass.callService("hue_music_sync", "set_options", {
        entity_id: area.switch,
        media_player: entityId || "",
      });
    }
    if (this._players) this._players.selected = entityId || null; // optimistic
    this._playersAt = 0; // and re-read once the integration has re-resolved
    this._playerMenuOpen = false;
    this._render();
  }

  /* -- play-the-beats drum pad --
     A page of three pads (Low / Mid / High). Each pad drives a third of the
     room's lights (by spectral order). While it's open the integration pauses
     its automatic beat-flashes, so your taps ARE the beats; the music's colour
     and energy keep flowing underneath. Backed by the hue_music_sync/tap and
     /drum WebSocket commands; the drum mode auto-expires server-side, kept alive
     by a heartbeat while this page is open. */
  _startDrums(m) {
    if (!this._cardNode || this._drum) return;
    const playing = !!(this._play && this._play.playing);

    const overlay = document.createElement("div");
    overlay.className = "hue-drum";

    // Header: back button + "Beat Pads" with the current track underneath.
    const head = document.createElement("div");
    head.className = "hue-drum-head";
    const back = document.createElement("button");
    back.className = "hue-drum-back";
    back.setAttribute("aria-label", "Back to the card");
    back.innerHTML =
      '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">' +
      '<path d="M15 5l-7 7 7 7" fill="none" stroke="currentColor" ' +
      'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    back.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      this._endDrums();
    });
    const titles = document.createElement("div");
    titles.className = "hue-drum-titles";
    const title = document.createElement("div");
    title.className = "hue-cal-title";
    title.textContent = "Beat Pads";
    titles.appendChild(title);
    const trackLine = m && m.now && m.now.track
      ? m.now.track + (m.now.artist ? ` · ${m.now.artist}` : "")
      : "";
    if (trackLine) {
      const tr = document.createElement("div");
      tr.className = "hue-drum-track";
      tr.textContent = trackLine;
      titles.appendChild(tr);
    }
    head.append(back, titles);

    const sub = document.createElement("div");
    sub.className = "hue-cal-sub";
    sub.textContent = playing
      ? "Tap in time, you drive the lights"
      : "Play a song first, then drum along";

    // Three tall tap columns; each drives a third of the room.
    const padWrap = document.createElement("div");
    padWrap.className = "hue-drum-pads";
    const groups = [
      // id, label, sub-label, default tint, glyph bar heights (px)
      ["low", "Low", "Bass", "#4ade80", [14, 30, 10, 5]],
      ["mid", "Mid", "Mids", "#58a6ff", [8, 22, 30, 7]],
      ["high", "High", "Treble", "#a78bfa", [5, 12, 26, 16, 7]],
    ];
    const padNodes = {};
    for (const [id, label, subLabel, tint, glyph] of groups) {
      const pad = document.createElement("button");
      pad.className = "hue-drum-pad";
      pad.disabled = !playing;
      pad.style.setProperty("--pad", tint);

      const tap = document.createElement("span");
      tap.className = "hue-drum-tap";
      tap.textContent = "Tap";

      const mid = document.createElement("span");
      mid.className = "hue-drum-mid";
      const bars = document.createElement("span");
      bars.className = "hue-drum-glyph";
      for (const h of glyph) {
        const bar = document.createElement("span");
        bar.style.height = `${h}px`;
        bars.appendChild(bar);
      }
      const dots = document.createElement("span");
      dots.className = "hue-drum-dots";
      for (let i = 0; i < 6; i++) dots.appendChild(document.createElement("span"));
      mid.append(bars, dots);

      const glow = document.createElement("span");
      glow.className = "hue-drum-glow";

      const lab = document.createElement("span");
      lab.className = "hue-drum-lab";
      const labMain = document.createElement("span");
      labMain.className = "hue-drum-pad-label";
      labMain.textContent = label;
      const labSub = document.createElement("span");
      labSub.className = "hue-drum-pad-sub";
      labSub.textContent = subLabel;
      lab.append(labMain, labSub);

      pad.append(glow, tap, mid, lab);
      if (playing) {
        pad.addEventListener("pointerdown", (e) => {
          e.preventDefault();
          this._drumTap(id);
        });
      }
      padWrap.appendChild(pad);
      padNodes[id] = pad;
    }

    const close = document.createElement("div");
    close.className = "hue-cal-cancel";
    close.textContent = "Done";
    close.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      this._endDrums();
    });

    overlay.append(head, sub, padWrap, close);
    this._cardNode.appendChild(overlay);
    this._drum = { overlay, pads: padNodes, playing, keep: 0 };

    if (playing) {
      this._sendDrum(true);
      // Heartbeat so the server-side drum window never lapses while we're open.
      this._drum.keep = setInterval(() => this._sendDrum(true), 2000);
      this._updateDrumTints();
    }
  }

  _drumTap(group) {
    const d = this._drum;
    if (!d) return;
    const pad = d.pads[group];
    if (pad) {
      pad.classList.remove("hit");
      void pad.offsetWidth; // restart the hit animation
      pad.classList.add("hit");
    }
    this._sendTap(group);
  }

  _endDrums() {
    const d = this._drum;
    if (!d) return;
    if (d.keep) clearInterval(d.keep);
    if (d.playing) this._sendDrum(false); // hand the beats back to the music
    d.overlay.remove();
    this._drum = null;
    this._render(); // repaint the normal card (state may have moved on)
  }

  _drumMessage(payload) {
    const area = this._areas[this._areaIndex] || {};
    const conn = this._hass && this._hass.connection;
    if (!conn || !area.switch || this._demo) return;
    try {
      conn.sendMessagePromise({ ...payload, entity_id: area.switch }).catch(() => {});
    } catch (_) { /* connection went away */ }
  }

  _sendTap(group) {
    this._drumMessage({ type: "hue_music_sync/tap", group });
  }

  _sendDrum(active) {
    this._drumMessage({ type: "hue_music_sync/drum", active });
  }

  // Tint each pad with the live colour of the lights it controls (same
  // left->right spectral split the integration uses for the groups).
  _updateDrumTints() {
    const d = this._drum;
    if (!d || !d.playing) return;
    const positions = this._liveMeta && this._liveMeta.positions;
    const lights = (this._live && this._live.lights) || {};
    if (!positions) return;
    const ids = Object.entries(positions)
      .sort((a, b) => a[1][0] - b[1][0])
      .map((e) => e[0]);
    const n = ids.length;
    if (!n) return;
    const a = Math.round(n / 3);
    const b = Math.round((2 * n) / 3);
    const groups = n < 3
      ? { low: ids, mid: ids, high: ids }
      : { low: ids.slice(0, a), mid: ids.slice(a, b), high: ids.slice(b) };
    for (const g of ["low", "mid", "high"]) {
      const pad = d.pads[g];
      if (!pad) continue;
      const hexes = groups[g].map((c) => lights[c]).filter(Boolean);
      if (hexes.length) pad.style.setProperty("--pad", this._avgHex(hexes));
    }
  }

  _avgHex(hexes) {
    let r = 0, g = 0, b = 0;
    for (const h of hexes) {
      r += parseInt(h.slice(1, 3), 16);
      g += parseInt(h.slice(3, 5), 16);
      b += parseInt(h.slice(5, 7), 16);
    }
    const k = hexes.length || 1;
    return rgbToHex(r / k, g / k, b / k);
  }

  /* ------------------------- Visualizer loop ------------------------- */
  _loop(now) {
    this._raf = requestAnimationFrame(this._loop);
    if (this._visible === false) return; // off-screen: skip all DOM work
    // The tablet card has no hero bars; it still animates the wash, cover,
    // live frequency map and waveform from the same viz, so don't bail here on
    // a missing bar set - just skip the bar-paint block below.

    // Lock the beat grid to playback: time advances only while the song plays,
    // from its reported position, so the bars pause/seek with the track.
    const p = this._play;
    let active, time;
    if (p) {
      active = p.on && p.playing;
      time = p.playing ? p.position + (Date.now() - p.updatedAt) / 1000 : p.position;
    } else {
      active = this._currentOn();
      time = now / 1000;
    }
    // Real audio when the live feed is fresh; simulation otherwise.
    const live = this._liveFresh();
    this._viz.step(active, time, p ? p.bpm : 122, p ? p.beatAnchor : null, live);
    this._applyStageLive(live);
    this._applyTimelineLive();
    if (this._drum) this._updateDrumTints();
    this._bloom *= 0.95;

    // Transport time readout (mm:ss / mm:ss).
    if (this._trTime) {
      const fmt = (s) => {
        s = Math.max(0, Math.floor(s));
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      };
      const dur = this._trDur || (this._liveMeta && this._liveMeta.duration) || 0;
      this._trTime.textContent = dur ? `${fmt(time)} / ${fmt(dur)}` : fmt(time);
    }

    if (this._barNodes && this._barNodes.length) {
      const colors = this._barColors || PALETTES[0].colors;
      const n = this._viz.count;
      const dim = this._barDim != null ? this._barDim : 0.9;
      for (let i = 0; i < n; i++) {
        const v = this._viz.levels[i];
        const c = colors[Math.floor((i / n) * colors.length) % colors.length];
        const node = this._barNodes[i];
        node.style.height = Math.min(100, Math.max(6, v * 100)) + "%";
        node.style.background = `linear-gradient(to top, ${c}, ${c}cc)`;
        node.style.opacity = dim;
        node.style.boxShadow = `0 0 ${(4 + v * 10).toFixed(1)}px ${c}66`;
      }
    }

    if (this._washNode) {
      const wash = this._washNode;
      wash.classList.toggle("idle", !active);
      if (active) {
        wash.style.opacity = (0.55 + this._viz.energy * 0.4).toFixed(3);
        if (!this._reduced) {
          // Musical motion: a small pulse per beat, a deeper breath on the
          // bar's downbeat, and a one-shot bloom when a loud section drops.
          const s = 1 + this._viz.beat * 0.025 + this._viz.downbeat * 0.045;
          wash.style.transform = `scale(${s.toFixed(4)})`;
          wash.style.filter =
            this._bloom > 0.02
              ? `blur(8px) brightness(${(1 + this._bloom * 0.7).toFixed(3)})`
              : "";
        }
      } else {
        wash.style.opacity = 0.22;
        wash.style.transform = "";
        wash.style.filter = "";
      }
    }
    if (this._glossNode) {
      this._glossNode.style.opacity = (0.5 + this._viz.beat * 0.3 + this._viz.downbeat * 0.2).toFixed(3);
    }
    this._loopExtra(active, time);
  }

  // Subclass hook: extra per-frame DOM work. No-op for the mobile card; the
  // tablet card drives its live frequency map and waveform playhead here.
  _loopExtra() {}

  _currentOn() {
    const area = this._areas[this._areaIndex] || {};
    if (area.switch && this._hass && this._hass.states[area.switch]) {
      return this._hass.states[area.switch].state === "on";
    }
    return this._ui.on;
  }
}

/* ============================================================================
   Tablet / landscape card ("Ambient Glow · Landscape")

   A landscape-optimised sibling of the mobile card, sharing its soft Hue-navy
   theme. It subclasses HueMusicSyncCard so it reuses ALL of the proven wiring -
   the entity model, the live analysis feed, album-art extraction, the beat-pad
   overlay, player selection and the visualizer loop - and overrides only the
   render (a two-column layout: now-playing on the left, controls on the right)
   plus a per-frame hook for its live frequency map and waveform playhead.
   ========================================================================== */

// Equalizer envelopes for the landscape intensity picker (per option, keyed by
// the normalised option label). Higher intensities move faster and taller.
const L_INT_ENV = {
  auto:    { spd: 1.25, max: 0.66, min: 0.20 },
  subtle:  { spd: 2.00, max: 0.44, min: 0.20 },
  medium:  { spd: 1.05, max: 0.70, min: 0.18 },
  high:    { spd: 0.52, max: 0.90, min: 0.13 },
  intense: { spd: 0.28, max: 1.00, min: 0.09 },
  extreme: { spd: 0.16, max: 1.00, min: 0.05 },
};
const L_INT_SHAPE = [0.58, 0.82, 1.0, 0.8, 0.6];
function lIntBars(label) {
  const e = L_INT_ENV[normalise(label)] || L_INT_ENV.medium;
  return L_INT_SHAPE.map((s, i) => {
    const max = Math.max(0.16, e.max * s);
    const min = Math.min(max - 0.05, e.min * (0.65 + 0.35 * s));
    return {
      min: min.toFixed(2), max: max.toFixed(2),
      spd: (e.spd * (0.84 + (i % 3) * 0.13)).toFixed(2) + "s",
      dly: (-(i * e.spd * 0.19)).toFixed(2) + "s",
    };
  });
}

// Landscape-only styles, appended after CARD_CSS in the tablet's shadow root so
// every shared component (pill, cover, fields, segmented, palette dots, slider,
// timing, power, area/player dropdowns) keeps the mobile card's exact look.
const TABLET_CSS = `
  /* container-type lets the breakpoint below react to the CARD's own width
     (a viewport media query can't - the card is often narrower than the page). */
  .hue-card.hue-land { padding: 0; container-type: inline-size; }

  .hue-land-top { position: relative; z-index: 3; display: flex; align-items: center;
    justify-content: space-between; padding: 22px 28px 6px; }
  .hue-land-top-right { display: flex; align-items: center; gap: 12px; }

  .hue-land-cols { position: relative; z-index: 2; display: grid; grid-template-columns: 0.9fr 1.1fr; }
  .hue-land-left { position: relative; padding: 10px 30px 30px; display: flex; flex-direction: column; justify-content: center; }
  .hue-land-left::after { content: ""; position: absolute; top: 8px; bottom: 8px; right: 0; width: 1px;
    background: linear-gradient(180deg, transparent, var(--hue-line) 18%, var(--hue-line) 82%, transparent); }
  .hue-land-right { position: relative; padding: 12px 30px 30px; display: flex; flex-direction: column;
    justify-content: space-between; gap: 14px; }
  .hue-land-right .hue-field { margin-bottom: 0; }

  /* now-playing block */
  .hue-land-cover { position: relative; align-self: center; margin: 6px 0 18px; }
  .hue-land-cover-glow { position: absolute; inset: -22px; border-radius: 44px; pointer-events: none; filter: blur(26px); transition: opacity .08s; }
  .hue-land-meta { text-align: center; margin-bottom: 18px; }
  .hue-land-meta .hue-now-track { font-size: 23px; }
  .hue-land-meta .hue-now-track-inner { display: inline-block; white-space: nowrap; }
  .hue-land-meta .hue-now-track-inner.scroll { animation: hue-mq 9s linear infinite alternate; }
  .hue-land-meta .hue-now-artist { font-size: 14.5px; margin-top: 3px; }

  /* centred transport */
  .hue-land-tx { display: flex; align-items: center; justify-content: center; gap: 20px; margin-bottom: 6px; }
  .hue-tx-btn { width: 46px; height: 46px; flex: none; border-radius: 50%;
    border: 1px solid rgba(255,255,255,.12); background: rgba(255,255,255,.08);
    color: rgba(255,255,255,.82); cursor: pointer; transition: .14s;
    display: flex; align-items: center; justify-content: center; touch-action: manipulation; }
  .hue-tx-btn:hover { background: rgba(255,255,255,.15); color: #fff; }
  .hue-tx-btn:active { transform: scale(.9); }
  .hue-tx-play { width: 60px; height: 60px; background: rgba(255,255,255,.12); }
  .hue-land-time { display: flex; justify-content: space-between; font-size: 11.5px; font-weight: 700;
    font-variant-numeric: tabular-nums; color: var(--hue-faint); margin-top: 4px; }

  /* waveform scrubber */
  .hue-wave { position: relative; display: flex; align-items: center; gap: 2px; height: 46px;
    margin: 10px 0 2px; }
  .hue-wave-bar { flex: 1; min-width: 0; border-radius: 2px; transition: background .25s, box-shadow .25s; }
  .hue-wave-cursor { position: absolute; top: 0; bottom: 0; width: 2px; border-radius: 2px; background: #fff;
    box-shadow: 0 0 10px #fff, 0 0 2px #fff; transform: translateX(-50%); pointer-events: none; }

  /* live frequency map */
  .hue-fmap { border: 1px solid rgba(255,255,255,.08); border-radius: 14px; background: rgba(0,0,0,.28);
    backdrop-filter: blur(4px); padding: 9px 14px 6px; margin-top: 12px; }
  .hue-fmap-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 2px; }
  .hue-fmap-live { display: flex; align-items: center; gap: 5px; font-size: 10px; font-weight: 800;
    letter-spacing: .1em; color: rgba(255,255,255,.5); }
  .hue-fmap-livedot { width: 6px; height: 6px; border-radius: 50%; flex: none; }
  .hue-fmap-rng { display: flex; align-items: center; gap: 6px; }
  .hue-fmap-rng-lbl { font-size: 9px; font-weight: 700; letter-spacing: .07em; color: rgba(255,255,255,.28); }
  .hue-fmap-rng-bar { width: 52px; height: 3px; border-radius: 3px; }
  .hue-fmap-stage { position: relative; height: 58px; }
  .hue-fmap-stage::after { content: ""; position: absolute; top: 50%; left: 0; right: 0; height: 1px;
    background: rgba(255,255,255,.05); pointer-events: none; }
  .hue-fmap-dot { position: absolute; width: 11px; height: 11px; border-radius: 50%; transform-origin: center;
    transform: translate(-50%, -50%); transition: box-shadow .09s, transform .09s, opacity .09s; }

  /* intensity picker · live equalizer */
  .hue-int-picker { display: flex; gap: 8px; }
  .hue-int-btn { position: relative; flex: 1; display: flex; flex-direction: column; align-items: center;
    gap: 10px; padding: 16px 4px; border: 1px solid rgba(255,255,255,.07); border-radius: 16px;
    background: rgba(255,255,255,.025); cursor: pointer; overflow: hidden; font-family: var(--hk);
    transition: border-color .18s, background .18s, box-shadow .18s, transform .12s; }
  .hue-int-btn:hover { background: rgba(255,255,255,.06); border-color: rgba(255,255,255,.13); }
  .hue-int-btn:active { transform: scale(.96); }
  .hue-int-btn.on { background: rgba(255,255,255,.05); }
  .hue-int-wash { position: absolute; inset: 0; pointer-events: none; opacity: 0;
    background: radial-gradient(85% 70% at 50% 122%, var(--ic), transparent 72%); transition: opacity .25s; }
  .hue-int-btn.on .hue-int-wash { opacity: .22; }
  .hue-int-eq { position: relative; z-index: 1; display: flex; align-items: flex-end; justify-content: center;
    gap: 3.5px; height: 30px; }
  .hue-int-bar { width: 4px; height: 100%; border-radius: 3px; flex: none; transform-origin: bottom;
    transform: scaleY(var(--min)); animation: hue-int-eq var(--spd) ease-in-out infinite alternate; animation-delay: var(--dly); }
  @keyframes hue-int-eq { from { transform: scaleY(var(--min)); } to { transform: scaleY(var(--max)); } }
  .hue-int-label { position: relative; z-index: 1; font-size: 12px; font-weight: 700; letter-spacing: .05em;
    color: rgba(255,255,255,.4); transition: color .18s; }
  .hue-int-btn.on .hue-int-label { color: rgba(255,255,255,.96); }
  .hue-int-underline { position: absolute; left: 50%; bottom: 0; width: 58%; height: 2.5px; border-radius: 3px;
    transform: translateX(-50%) scaleX(0); transform-origin: center;
    transition: transform .24s cubic-bezier(.2,1.3,.3,1); }
  .hue-int-btn.on .hue-int-underline { transform: translateX(-50%) scaleX(1); }

  @media (prefers-reduced-motion: reduce) {
    .hue-int-bar { animation: none; transform: scaleY(var(--max)); }
    .hue-land-meta .hue-now-track-inner.scroll { animation: none !important; }
  }

  /* When the card itself is narrow (a small dashboard column, portrait split,
     or a phone), stack the two columns instead of clipping. Keyed off the
     card's own width via the container query above, not the viewport. */
  @container (max-width: 760px) {
    .hue-land-cols { grid-template-columns: 1fr; }
    .hue-land-left::after { display: none; }
    .hue-land-left { border-bottom: 1px solid var(--hue-line); }
    .hue-land-meta .hue-now-track { font-size: 20px; }
  }
`;

class HueMusicSyncTabletCard extends HueMusicSyncCard {
  getCardSize() { return 10; }

  // Sections view: claim the full section width by default (this is a landscape
  // card - a half-width column would clip its two columns). Users can still
  // resize it down; it never drops below half, and it stacks below 760px.
  getGridOptions() {
    return { columns: "full", rows: "auto", min_columns: 6 };
  }

  // Older "layout card" / masonry hint of the same intent (harmless where the
  // view ignores it; masonry has no true column-spanning, so a Sections or
  // Panel view is where this card gets its width).
  getLayoutOptions() {
    return { grid_columns: 4, grid_rows: "auto" };
  }

  static getStubConfig() {
    const base = HueMusicSyncCard.getStubConfig();
    return { ...base, type: "custom:hue-music-sync-card-tablet" };
  }

  /* -- landscape render (overrides the mobile hero+body layout) -- */
  _renderImpl() {
    if (!this._config) return;
    // Don't tear the DOM down while the beat-pad overlay is open.
    if (this._drum) return;
    const m = this._model();
    const accent = m.accent;
    const pal = m.colour.selected;
    const wc = pal.colors;

    // Same pre-render bookkeeping the mobile card does: album extraction and a
    // playback snapshot so the shared visualizer loop stays locked to the song.
    this._maybeExtractAlbum(m);
    this._play = {
      on: m.on,
      playing: m.now.playing,
      position: m.now.position || 0,
      updatedAt: m.now.updatedAt || Date.now(),
      bpm: m.bpm > 0 ? m.bpm : 122,
      beatAnchor: m.beatAnchor,
    };
    this._accent = accent;

    // Reset the mobile-only per-render node refs the shared loop / sync methods
    // touch, so a prior mobile render (or an earlier tablet render) can't leave
    // them pointing at detached nodes.
    this._barNodes = null;
    this._heroArtNode = null;
    this._trTime = null;
    this._stageNode = null;
    this._stageDots = null;
    this._tlNode = null;
    this._tlSecs = null;

    const card = document.createElement("div");
    card.className = "hue-card hue-land";
    this._cardNode = card;
    // Album/palette hue through the whole landscape card, not just a faint band
    // at the very top — the song's colour reads as a hue inside the card.
    card.style.background = this._albumTintBackground(wc, "#171626", "#0f0e1c");
    card.style.boxShadow = m.on
      ? `0 34px 90px -28px ${accent}66, 0 0 0 1px var(--hue-line)`
      : "0 0 0 1px var(--hue-line)";

    // Soft palette wash across the top (pulsed by the shared loop via _washNode).
    const wash = document.createElement("div");
    wash.className = "hue-hero-wash";
    wash.style.background =
      `radial-gradient(50% 120% at 10% -12%, ${wc[0]}d8, transparent 55%),` +
      `radial-gradient(50% 120% at 92% -12%, ${wc[wc.length - 1]}cc, transparent 55%)`;
    this._washNode = wash;
    card.appendChild(wash);

    card.appendChild(this._landTop(m, accent));

    const cols = document.createElement("div");
    cols.className = "hue-land-cols";
    cols.appendChild(this._landLeft(m, accent, wc));
    cols.appendChild(this._landRight(m, accent, pal));
    card.appendChild(cols);

    this.shadowRoot.innerHTML = `<style>${CARD_CSS}${TABLET_CSS}</style>`;
    this.shadowRoot.appendChild(card);

    // Apply artwork to the cover (there is no full-bleed hero art in landscape),
    // (re)connect the live feed and set up the title marquee.
    this._applyArt(m.now.art);
    this._ensureLiveSub();
    this._setupMarquee();
  }

  /* -- top bar: source pill + brightness readout + power -- */
  _landTop(m, accent) {
    const top = document.createElement("div");
    top.className = "hue-land-top";

    const pill = document.createElement("div");
    pill.className = "hue-pill";
    pill.style.boxShadow = m.on ? `inset 0 0 0 1px ${accent}66` : "";
    const dot = document.createElement("span");
    dot.className = "hue-pill-dot";
    const SRC_LABEL = {
      "live-tap": "Live audio", snapcast: "Live (snapcast)",
      "track-map": "Track map", metadata: "Metadata only", idle: "Starting",
    };
    const onFallback = m.on && m.audioSource === "metadata";
    const pillColor = !m.on ? "#6b7088" : onFallback ? "#ffb24d" : accent;
    dot.style.background = pillColor;
    dot.style.boxShadow = m.on ? `0 0 8px ${pillColor}` : "none";
    pill.appendChild(dot);
    pill.appendChild(document.createTextNode(
      m.on ? (SRC_LABEL[m.audioSource] || "Streaming") : "Idle"
    ));
    top.appendChild(pill);

    const right = document.createElement("div");
    right.className = "hue-land-top-right";
    const bright = document.createElement("div");
    bright.className = "hue-bright-mini";
    bright.innerHTML =
      `<span class="hue-bright-mini-icon">☀</span><span>${Math.round(m.brightness.value)}%</span>`;
    right.appendChild(bright);
    right.appendChild(this._power(m, accent));
    top.appendChild(right);
    return top;
  }

  /* -- left column: cover, meta, transport, waveform, live freq map -- */
  _landLeft(m, accent, wc) {
    const left = document.createElement("div");
    left.className = "hue-land-left";

    // Big centred album cover with a soft colour halo behind it.
    const coverWrap = document.createElement("div");
    coverWrap.className = "hue-land-cover";
    const glow = document.createElement("div");
    glow.className = "hue-land-cover-glow";
    glow.style.background =
      `radial-gradient(circle, ${wc[0]}55 0%, ${wc[wc.length - 1]}2e 58%, transparent 74%)`;
    this._landCoverGlow = glow;
    coverWrap.appendChild(glow);
    coverWrap.appendChild(this._cover(208, 26, m.now.art));
    left.appendChild(coverWrap);

    // Track / artist (marquee for long titles, like the mobile card).
    const meta = document.createElement("div");
    meta.className = "hue-land-meta";
    const track = document.createElement("div");
    track.className = "hue-now-track";
    const trackInner = document.createElement("span");
    trackInner.className = "hue-now-track-inner";
    trackInner.textContent = m.now.track;
    track.appendChild(trackInner);
    this._marqueeNodes = [track, trackInner];
    const artist = document.createElement("div");
    artist.className = "hue-now-artist";
    artist.textContent = m.now.artist;
    meta.append(track, artist);
    left.appendChild(meta);

    left.appendChild(this._landTransport(m));

    // Waveform scrubber + time readout, driven per-frame by _loopExtra.
    const wave = this._waveform(m, wc, accent);
    left.appendChild(wave);
    const time = document.createElement("div");
    time.className = "hue-land-time";
    const tCur = document.createElement("span");
    const tDur = document.createElement("span");
    tDur.textContent = this._fmtTime(m.now.duration || 0);
    time.append(tCur, tDur);
    this._waveTimeCur = tCur;
    left.appendChild(time);

    left.appendChild(this._freqMap(accent, wc));
    return left;
  }

  /* -- right column: area/player, intensity, effect, colour, brightness+timing -- */
  _landRight(m, accent, pal) {
    const right = document.createElement("div");
    right.className = "hue-land-right";

    // Area + player selectors (reuse the mobile dropdowns for a consistent feel).
    const selects = document.createElement("div");
    selects.className = "hue-grid2";
    selects.style.gap = "0 16px";
    const areaField = document.createElement("div");
    areaField.className = "hue-field";
    areaField.appendChild(this._label("Area", null));
    areaField.appendChild(this._areaSelect(accent));
    selects.appendChild(areaField);
    const playerDd = this._playerSelect(accent);
    if (playerDd) {
      const playerField = document.createElement("div");
      playerField.className = "hue-field";
      playerField.appendChild(this._label("Player", null));
      playerField.appendChild(playerDd);
      selects.appendChild(playerField);
    }
    right.appendChild(selects);

    // Intensity - landscape equalizer picker.
    const intField = document.createElement("div");
    intField.className = "hue-field";
    const intHint = m.intensity.autoMode
      ? `Auto · ${titleize(m.intensity.autoMode)}` : null;
    const selInt = m.intensity.options.find((o) => o.value === m.intensity.value);
    intField.appendChild(this._label("Intensity", intHint || (selInt ? selInt.label : "")));
    intField.appendChild(this._intensityPicker(m, accent));
    // Auto: the enabled-rungs checklist, same control as the mobile card.
    if (m.intensity.value === "auto") {
      intField.appendChild(this._autoRangeDropdown(m, accent));
    }
    right.appendChild(intField);
    right.appendChild(this._advancedSection(m, accent));

    // Effect - shared segmented control.
    const effLabel = m.effect.options.find((o) => o.value === m.effect.value);
    const effField = document.createElement("div");
    effField.className = "hue-field";
    effField.appendChild(this._label("Effect", effLabel ? effLabel.label : ""));
    effField.appendChild(this._segmented(m.effect.options, m.effect.value, accent, (v) => {
      this._callSelect(m.effect.entity, v, "effect");
      this._render();
    }));
    right.appendChild(effField);

    // Colour - shared palette dots.
    const colField = document.createElement("div");
    colField.className = "hue-field";
    colField.appendChild(this._label("Colour", pal.name));
    colField.appendChild(this._dots(m.colour.options, m.colour.value, (v) => {
      this._callSelect(m.colour.entity, v, "colour");
      this._render();
    }));
    right.appendChild(colField);

    // Brightness + timing (timing already carries the beat-pad button).
    const grid = document.createElement("div");
    grid.className = "hue-grid2 tight";
    const brightField = document.createElement("div");
    brightField.className = "hue-field";
    brightField.appendChild(this._label("Brightness"));
    brightField.appendChild(this._slider(m, accent));
    grid.appendChild(brightField);
    const timingField = document.createElement("div");
    timingField.className = "hue-field";
    timingField.appendChild(this._label("Timing"));
    timingField.appendChild(this._timing(m, accent));
    grid.appendChild(timingField);
    right.appendChild(grid);
    return right;
  }

  /* -- centred circular transport (only when a live player is followed) -- */
  _landTransport(m) {
    const tx = document.createElement("div");
    tx.className = "hue-land-tx";
    if (!m.now.player) return tx; // nothing to control yet
    const svc = (service, data) => {
      if (this._hass) {
        this._hass.callService("media_player", service, { entity_id: m.now.player, ...data });
      }
    };
    const ICONS = {
      prev: "M6 5h2v14H6zM20 5v14l-11-7z",
      play: "M8 5l12 7-12 7z",
      pause: "M7 5h4v14H7zM13 5h4v14h-4z",
      next: "M16 5h2v14h-2zM4 5l11 7-11 7z",
    };
    const mk = (icon, service, label, play) => {
      const b = document.createElement("button");
      b.className = "hue-tx-btn" + (play ? " hue-tx-play" : "");
      const size = play ? 24 : 18;
      b.innerHTML =
        `<svg viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true">` +
        `<path d="${ICONS[icon]}" fill="currentColor"/></svg>`;
      b.setAttribute("aria-label", label);
      b.addEventListener("click", () => svc(service));
      return b;
    };
    tx.appendChild(mk("prev", "media_previous_track", "Previous track"));
    tx.appendChild(mk(m.now.playing ? "pause" : "play", "media_play_pause", "Play / pause", true));
    tx.appendChild(mk("next", "media_next_track", "Next track"));
    return tx;
  }

  /* -- waveform (stable pseudo-random silhouette; view-only playhead + section
     highlights). Deliberately NOT a scrubber: it must not seek the player, so a
     stray tap can't jump the song. -- */
  _waveform(m, colors, accent) {
    const N = 68;
    if (!this._waveShape) {
      const a = [];
      for (let i = 0; i < N; i++) {
        const env = 0.45 + 0.4 * Math.sin(i * 0.19) + 0.18 * Math.sin(i * 0.052);
        const grit = 0.5 + 0.5 * Math.sin(i * 2.7 + Math.sin(i * 0.9) * 3);
        a.push(Math.max(0.14, Math.min(1, env * (0.55 + grit * 0.5))));
      }
      this._waveShape = a;
    }
    const wave = document.createElement("div");
    wave.className = "hue-wave";
    const bars = [];
    this._waveShape.forEach((v, i) => {
      const bar = document.createElement("div");
      bar.className = "hue-wave-bar";
      bar.style.height = `${14 + v * 86}%`;
      const c = colors[Math.floor((i / N) * colors.length) % colors.length];
      wave.appendChild(bar);
      bars.push({ node: bar, frac: i / N, c });
    });
    const cursor = document.createElement("div");
    cursor.className = "hue-wave-cursor";
    wave.appendChild(cursor);

    this._waveBars = bars;
    this._waveCursor = cursor;
    this._waveDur = m.now.duration || 0;
    this._waveLastIdx = -1;
    // View-only: no pointer/seek handlers. The playhead + section highlights are
    // driven by the live loop; the waveform never moves the track.
    return wave;
  }

  /* -- live frequency map: one dot per lamp (or a default row) low -> high -- */
  _freqMap(accent, colors) {
    const map = document.createElement("div");
    map.className = "hue-fmap";

    const head = document.createElement("div");
    head.className = "hue-fmap-head";
    const live = document.createElement("span");
    live.className = "hue-fmap-live";
    const ld = document.createElement("span");
    ld.className = "hue-fmap-livedot";
    ld.style.background = accent;
    ld.style.boxShadow = `0 0 6px ${accent}`;
    live.append(ld, document.createTextNode("LIVE"));
    const rng = document.createElement("div");
    rng.className = "hue-fmap-rng";
    const lo = document.createElement("span");
    lo.className = "hue-fmap-rng-lbl";
    lo.textContent = "LOW";
    const rbar = document.createElement("div");
    rbar.className = "hue-fmap-rng-bar";
    rbar.style.background = `linear-gradient(90deg, ${[0, 0.25, 0.5, 0.75, 1].map((t) => spectrumColor(t)).join(", ")})`;
    const hi = document.createElement("span");
    hi.className = "hue-fmap-rng-lbl";
    hi.textContent = "HIGH";
    rng.append(lo, rbar, hi);
    head.append(live, rng);
    map.appendChild(head);

    const stage = document.createElement("div");
    stage.className = "hue-fmap-stage";
    map.appendChild(stage);

    this._fmapStage = stage;
    this._fmapDots = null; // built lazily in _loopExtra (lamp count may change)
    this._fmapN = 0;
    return map;
  }

  // (Re)build the freq-map dots when the live lamp count changes. Uses the real
  // room layout (from the live meta feed) laid out left->high; falls back to a
  // fixed row before the feed arrives.
  _ensureFmapDots(n) {
    if (this._fmapN === n && this._fmapDots) return;
    const stage = this._fmapStage;
    if (!stage) return;
    stage.replaceChildren();
    this._fmapN = n;
    this._fmapDots = [];
    for (let i = 0; i < n; i++) {
      const dot = document.createElement("div");
      dot.className = "hue-fmap-dot";
      const fx = n > 1 ? i / (n - 1) : 0.5;
      dot.style.left = `${(6 + fx * 86).toFixed(1)}%`;
      dot.style.top = i % 2 === 0 ? "26%" : "70%";
      const col = spectrumColor(fx);
      dot.style.background = col;
      stage.appendChild(dot);
      this._fmapDots.push({ node: dot, fx, col });
    }
  }

  /* -- intensity equalizer picker -- */
  _intensityPicker(m, accent) {
    const picker = document.createElement("div");
    picker.className = "hue-int-picker";
    m.intensity.options.forEach((opt) => {
      const on = opt.value === m.intensity.value;
      const btn = document.createElement("button");
      btn.className = "hue-int-btn" + (on ? " on" : "");
      if (on) {
        btn.style.borderColor = `${accent}66`;
        btn.style.boxShadow = `0 0 24px ${accent}22, inset 0 0 0 1px ${accent}2e`;
      }
      const wash = document.createElement("span");
      wash.className = "hue-int-wash";
      wash.style.setProperty("--ic", accent);
      btn.appendChild(wash);

      const eq = document.createElement("div");
      eq.className = "hue-int-eq";
      lIntBars(opt.label).forEach((b) => {
        const bar = document.createElement("span");
        bar.className = "hue-int-bar";
        bar.style.setProperty("--min", b.min);
        bar.style.setProperty("--max", b.max);
        bar.style.setProperty("--spd", b.spd);
        bar.style.setProperty("--dly", b.dly);
        bar.style.background = on ? accent : "rgba(255,255,255,.30)";
        bar.style.boxShadow = on ? `0 0 8px ${accent}aa` : "none";
        eq.appendChild(bar);
      });
      btn.appendChild(eq);

      const lab = document.createElement("span");
      lab.className = "hue-int-label";
      lab.textContent = opt.label;
      btn.appendChild(lab);

      const underline = document.createElement("span");
      underline.className = "hue-int-underline";
      underline.style.background = accent;
      underline.style.boxShadow = `0 0 10px ${accent}`;
      btn.appendChild(underline);

      btn.addEventListener("click", () => {
        this._callSelect(m.intensity.entity, opt.value, "intensity");
        this._render();
      });
      picker.appendChild(btn);
    });
    return picker;
  }

  _fmtTime(s) {
    s = Math.max(0, Math.floor(s));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  /* -- per-frame: waveform playhead + live freq-map (called from the loop) -- */
  _loopExtra(active) {
    // Cover halo breathes with the beat.
    if (this._landCoverGlow) {
      this._landCoverGlow.style.opacity = (0.55 + this._viz.beat * 0.45).toFixed(3);
    }

    // Waveform playhead: position advances only while the song plays.
    if (this._waveBars && this._waveCursor) {
      const p = this._play;
      let pos = 0;
      if (p) pos = p.playing ? p.position + (Date.now() - p.updatedAt) / 1000 : p.position;
      const dur = this._waveDur || (this._liveMeta && this._liveMeta.duration) || 0;
      const pct = dur > 0 ? Math.max(0, Math.min(1, pos / dur)) : 0;
      this._waveCursor.style.left = (pct * 100).toFixed(2) + "%";
      // Repaint the played/unplayed split only when the boundary bar changes.
      const idx = Math.round(pct * this._waveBars.length);
      if (idx !== this._waveLastIdx) {
        this._waveLastIdx = idx;
        this._waveBars.forEach((b, i) => {
          const played = i < idx;
          b.node.style.background = played
            ? `linear-gradient(to top, ${b.c}, ${b.c}cc)`
            : "#ffffff1a";
          b.node.style.boxShadow = played ? `0 0 6px ${b.c}55` : "none";
        });
      }
      if (this._waveTimeCur) this._waveTimeCur.textContent = this._fmtTime(pos);
    }

    // Live frequency map: one glowing dot per lamp, energy from the viz bands.
    if (this._fmapStage) {
      const positions = this._liveMeta && this._liveMeta.positions;
      const lampCount = positions ? Object.keys(positions).length : 0;
      const n = lampCount || 5;
      this._ensureFmapDots(n);
      const L = this._viz.levels;
      const dots = this._fmapDots || [];
      for (let i = 0; i < dots.length; i++) {
        const d = dots[i];
        const start = Math.floor((i / dots.length) * L.length);
        const end = Math.max(start + 1, Math.floor(((i + 1) / dots.length) * L.length));
        let e = 0;
        for (let j = start; j < end; j++) e += L[j];
        e = active ? e / (end - start) : 0.05;
        d.node.style.boxShadow = `0 0 ${(4 + e * 26).toFixed(0)}px ${d.col}`;
        d.node.style.opacity = (0.32 + e * 0.68).toFixed(3);
        d.node.style.transform = `translate(-50%, -50%) scale(${(0.78 + e * 0.58).toFixed(3)})`;
      }
    }
  }
}

const defineCard = () => {
  if (!customElements.get("hue-music-sync-card")) {
    try {
      customElements.define("hue-music-sync-card", HueMusicSyncCard);
    } catch (e) {
      console.error("hue-music-sync-card: define failed", e);
    }
  }
  if (!customElements.get("hue-music-sync-card-tablet")) {
    try {
      customElements.define("hue-music-sync-card-tablet", HueMusicSyncTabletCard);
    } catch (e) {
      console.error("hue-music-sync-card-tablet: define failed", e);
    }
  }
  console.info(
    `hue-music-sync-card: registered=${!!customElements.get("hue-music-sync-card")}` +
    ` tablet=${!!customElements.get("hue-music-sync-card-tablet")}` +
    ` top=${window === window.top}`
  );
};

// DO NOT define eagerly. HA's core bundle installs a scoped custom-element-
// registry polyfill as it boots; a definition made BEFORE that polyfill
// installs lands in the native registry the polyfill cannot see -
// customElements.get() then returns undefined for us forever and every
// dashboard shows "Custom element doesn't exist", while a page where this
// module happened to load AFTER the core bundle works fine. That is exactly
// the race a cached app shell loses: this module (small, cached) executes
// before HA's core chunk. Diagnosed live: registered=true at module load
// flipping to defined=false one second later as the polyfill replaced the
// registry. So: register only once HA's own root element exists (by then the
// registry is final), with a timer fallback for non-HA contexts (the demo
// harness, plain previews).
if (customElements.get("home-assistant")) {
  defineCard();
} else {
  const fallback = setTimeout(defineCard, 5000);
  customElements.whenDefined("home-assistant").then(() => {
    clearTimeout(fallback);
    defineCard();
  });
}

// Register in the dashboard card picker (guarded against double-loading, since
// the integration may expose the card via both a Lovelace resource and an extra
// JS module).
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "hue-music-sync-card")) {
  window.customCards.push({
    type: "hue-music-sync-card",
    name: "Hue Synco Card",
    description: "Ambient Glow card for the Hue Synco integration.",
    preview: true,
    documentationURL: "https://github.com/engabd11/syncoV2",
  });
}
if (!window.customCards.some((c) => c.type === "hue-music-sync-card-tablet")) {
  window.customCards.push({
    type: "hue-music-sync-card-tablet",
    name: "Hue Synco Card (Tablet)",
    description: "Landscape Ambient Glow card for the Hue Synco integration, optimised for tablets.",
    preview: true,
    documentationURL: "https://github.com/engabd11/syncoV2",
  });
}

// Late-load self-heal, scoped strictly to THIS document. If a dashboard
// rendered before this module executed (slow resource import, stale app
// shell), Lovelace shows "Custom element doesn't exist" error cards. Modern
// HA rebuilds those itself via customElements.whenDefined -> ll-rebuild; this
// covers two gaps in our own realm only: (a) the registry was swapped after
// an early define, so re-register with the current one; (b) nudge our own
// document's stale error cards with ll-rebuild (the event must be dispatched
// on the error element itself, which lives behind nested shadow roots, hence
// the deep walk; rebuilding an unrelated error card is a harmless no-op).
// Dashboards in other frames must import the module themselves - reaching
// into sibling frames or injecting scripts is deliberately NOT done here.
const healLateLoad = () => {
  try {
    const defined =
      !!customElements.get("hue-music-sync-card") &&
      !!customElements.get("hue-music-sync-card-tablet");
    if (!defined) defineCard();
    const errors = [];
    const walk = (node) => {
      for (const el of node.querySelectorAll("*")) {
        if (el.localName === "hui-error-card") errors.push(el);
        if (el.shadowRoot) walk(el.shadowRoot);
      }
    };
    walk(document);
    for (const el of errors) {
      el.dispatchEvent(new Event("ll-rebuild", { bubbles: true, composed: true }));
    }
    if (errors.length || !defined) {
      console.info(`hue-music-sync-card heal: error-cards=${errors.length} defined=${defined}`);
    }
  } catch (e) {
    /* healing is best-effort; never break the page over it */
  }
};
for (const delay of [800, 3000, 8000]) setTimeout(healLateLoad, delay);

console.info(
  `%c HUE-MUSIC-SYNC-CARD %c ${VERSION} `,
  "color:#fff;background:#7b5cff;font-weight:700;border-radius:4px 0 0 4px;padding:2px 4px",
  "color:#7b5cff;background:#1d1c30;border-radius:0 4px 4px 0;padding:2px 4px"
);
