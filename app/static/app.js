"use strict";

const body = document.body;
const MIN_DB = Number(body.dataset.minDb);
const MAX_DB = Number(body.dataset.maxDb);
const IS_MOCK = body.dataset.backend === "mock";
const PEAK_BUCKETS = 2000; // finer buckets so the waveform still looks good when zoomed in
const MIN_SPAN = 1; // most zoomed in: 1 second across the whole waveform
const MASTER = "__master__";
const LIVE_STEP = 0.1;

const $ = (id) => document.getElementById(id);
const el = {
  connText: $("conn-text"), conn: $("conn"),
  time: $("time"), total: $("total"), status: $("status"), wave: $("wave"),
  goto: $("goto"), gotoInput: $("goto-input"), gotoBtn: $("goto-btn"),
  zoomIn: $("zoom-in"), zoomOut: $("zoom-out"), zoomFit: $("zoom-fit"), pan: $("pan"), viewInfo: $("view-info"),
  now: $("now-take"), start: $("btn-start"), rec: $("btn-rec"), play: $("btn-play"), stop: $("btn-stop"),
  hint: $("rec-hint"), tracks: $("tracks"), takes: $("takes"), toast: $("toast"),
  confirm: $("confirm"), confirmName: $("confirm-name"), confirmSkip: $("confirm-skip"),
  confirmYes: $("confirm-yes"), confirmNo: $("confirm-no"),
};

const STATUS_LABEL = { stopped: "Leállítva", playing: "Lejátszás", paused: "Szünet", recording: "Felvétel" };

const state = {
  transport: { status: "stopped", position: 0, length: 0 },
  receivedAt: performance.now(), // when `transport` arrived, to interpolate the clock between polls
  tracks: [],
  projects: [], 
  inputOptions: [],
  projectsSig: "",
  live: { buckets: [] },
  hwOutputs: [], // selectable hardware outputs, e.g. ["Out 1/2", ..., "Out 1", ...]
  seekPreview: null, // seconds while dragging on the waveform
  view: { auto: true, start: 0, span: 60, follow: true }, // visible window of the waveform, in seconds
  panAt: -1e9, // last time the scroll slider was used
  takes: [], // normalized takes, see "takes" section
  takesSig: "",
  takesEpoch: 0, // minden take-módosítás növeli; az előtte indult lekérdezés elavult válaszát eldobjuk
  starPending: new Map(), // név -> csillag állapot, amíg a kérés fut (az újrarajzolás ne írja felül)
  stopAt: null, // másodperc: ha lejátszás közben ezt eléri a fej, megállunk (take ▶ gomb)
  recStart: null, // ahol az aktuális felvétel indult
  renaming: false, // inline átnevezés folyik: a lekérdezés ne rajzolja újra a listát
  takesBusy: 0, // > 0 while a take change is being sent (don't let a poll overwrite it)
  peaks: { length: 0, peaks: [] },
  peaksSig: "",
  peaksVersion: 0,
  peaksLoading: false,
};

/* ---------- helpers ---------- */

function pad2(n) {
  return String(n).padStart(2, "0");
}

function fmt(seconds) {
  const cs = Math.floor(Math.max(0, seconds) * 100);
  return `${Math.floor(cs / 6000)}:${pad2(Math.floor(cs / 100) % 60)}.${pad2(cs % 100)}`;
}

/** 300 -> "5:00" */
function fmtShort(seconds) {
  const total = Math.round(Math.max(0, seconds));
  return `${Math.floor(total / 60)}:${pad2(total % 60)}`;
}

/** "1:23.45", "1:02:03", "83.5", "83,5" -> seconds, or null when it can't be read. */
function parseTime(text) {
  const parts = text.trim().replace(",", ".").split(":");
  if (parts.length > 3 || parts.some((p) => !/^\d+(\.\d+)?$/.test(p))) return null;
  return parts.reduce((total, p) => total * 60 + Number(p), 0);
}

function paintRange(input) {
  const [min, max, value] = [Number(input.min), Number(input.max), Number(input.value)];
  input.style.setProperty("--fill", `${max > min ? ((value - min) / (max - min)) * 100 : 0}%`);
}

function throttle(fn, ms) {
  let last = 0, timer = null, lastArgs = [];
  return (...args) => {
    lastArgs = args;
    const wait = ms - (performance.now() - last);
    if (wait <= 0) {
      last = performance.now();
      fn(...args);
    } else if (!timer) {
      timer = setTimeout(() => { timer = null; last = performance.now(); fn(...lastArgs); }, wait);
    }
  };
}

/** True when `row.dataset[key]` holds a timestamp younger than `ms`. */
function recent(row, key, ms = 1500) {
  const t = Number(row.dataset[key]);
  return t > 0 && performance.now() - t < ms;
}

let toastTimer;
function toast(message, kind = "err") {
  el.toast.textContent = message;
  el.toast.className = `show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.toast.classList.remove("show"), kind === "ok" ? 4000 : 6000);
}

async function api(method, url, payload) {
  const res = await fetch(url, {
    method,
    headers: payload ? { "Content-Type": "application/json" } : undefined,
    body: payload ? JSON.stringify(payload) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && url !== "/api/login") showLogin();
  if (!res.ok) {
    const err = new Error(data.error || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

/** POST a transport command; the response is the new transport state. */
async function act(url, payload) {
  try {
    applyTransport(await api("POST", url, payload));
    return true;
  } catch (err) {
    toast(err.message);
    return false;
  }
}

/** POST a track setting (arm, mute, solo, output...). Returns whether it worked. */
async function post(url, payload) {
  try {
    await api("POST", url, payload);
    return true;
  } catch (err) {
    toast(err.message);
    return false;
  }
}

function setButton(button, icon, label) {
  button.querySelector(".ico").textContent = icon;
  button.querySelector(".lbl").textContent = label;
}

/** Mentés / megnyitás / projektváltás: offline vagy felvétel közben tiltva. */
function syncProjectButtons() {
  const offline = body.classList.contains("offline");
  const recording = state.transport.status === "recording";
  $("btn-open").disabled = recording;
  ["btn-save", "proj-btn"].forEach((id) => { $(id).disabled = offline || recording; });
  if (offline || recording) projList.hidden = true;
}

let favicon = $("favicon");
const FAVICON_ONLINE = favicon.getAttribute("href");
const FAVICON_OFFLINE = favicon.dataset.offline;
const spin = { frames: [], loading: null, running: false, wanted: false, timer: null, i: 0 };

function setFaviconHref(href) {
  if (favicon.getAttribute("href") === href) return;
  const link = favicon.cloneNode(); // a <link> cseréje kell, különben a böngésző nem tölti újra
  link.setAttribute("href", href);
  favicon.replaceWith(link);
  favicon = link;
}

/** A GIF képkockái 64x64-es PNG-kként (egyszer töltődik be). Ha a böngésző nem tudja, marad a sima GIF. */
function loadSpinFrames() {
  spin.loading ??= (async () => {
    if (!("ImageDecoder" in window)) return;
    try {
      const data = await (await fetch(FAVICON_OFFLINE)).arrayBuffer();
      const decoder = new ImageDecoder({ data, type: "image/gif" });
      await decoder.tracks.ready;
      await decoder.completed;
      const count = decoder.tracks.selectedTrack.frameCount;
      const canvas = Object.assign(document.createElement("canvas"), { width: 64, height: 64 });
      const g = canvas.getContext("2d");
      for (let i = 0; i < count; i++) {
        const { image } = await decoder.decode({ frameIndex: i });
        const scale = Math.min(64 / image.displayWidth, 64 / image.displayHeight);
        const w = image.displayWidth * scale, h = image.displayHeight * scale;
        g.clearRect(0, 0, 64, 64);
        g.drawImage(image, (64 - w) / 2, (64 - h) / 2, w, h);
        spin.frames.push({ href: canvas.toDataURL("image/png"), ms: Math.max(20, (image.duration || 100000) / 1000) });
        image.close();
      }
    } catch (err) {
      console.warn("GIF képkockák nem olvashatók, marad a sima GIF:", err);
    }
  })();
  return spin.loading;
}

async function startSpin() {
  await loadSpinFrames();
  if (!spin.wanted || spin.running) return; // közben visszajött a REAPER
  if (!spin.frames.length) return setFaviconHref(FAVICON_OFFLINE);
  spin.running = true;
  const total = spin.frames.reduce((sum, f) => sum + f.ms, 0);
  const t0 = performance.now();
  let last = -1;
  spin.timer = setInterval(() => {
    let t = (performance.now() - t0) % total, i = 0;
    while (t >= spin.frames[i].ms) t -= spin.frames[i++].ms;
    if (i === last) return;
    last = i;
    favicon.setAttribute("href", spin.frames[i].href); // gyors út: nem cseréljük a <link>-et
  }, 20);
}

function stopSpin() {
  spin.running = false;
  clearInterval(spin.timer);
  setFaviconHref(FAVICON_ONLINE);
}


/** Offline: pörgő macska a tabon; online: vissza a normál ikon. */
function setFavicon(offline) {
  if (spin.wanted === offline) return;
  spin.wanted = offline;
  if (offline) startSpin();
  else stopSpin();
}

const offlineCard = $("offline-card");
const offlineSound = $("offline-sound");
const offlineGif = $("offline-gif");
const offlineOpen = $("offline-open");
let wasOffline = false;

let pendingOfflineSound = false;
let offlineDebounce = null;

// ---------- Web Audio: előre dekódolt hang, azonnali indulás ----------
const AudioCtx = window.AudioContext || window.webkitAudioContext;
let audioCtx = null;
let soundBuffer = null;
let soundSource = null;
let soundPlaying = false;

async function preloadOfflineSound() {
  if (!AudioCtx || !offlineSound) return;
  try {
    audioCtx = new AudioCtx();
    const res = await fetch(offlineSound.currentSrc || offlineSound.src);
    const data = await res.arrayBuffer();
    soundBuffer = await new Promise((ok, err) => audioCtx.decodeAudioData(data, ok, err));
  } catch (e) {
    soundBuffer = null; // marad az <audio> tartalék
  }
}
preloadOfflineSound();

function playOfflineSound() {
  if (audioCtx && soundBuffer) {
    if (audioCtx.state !== "running") { pendingOfflineSound = true; return; }
    stopOfflineSound();
    const src = audioCtx.createBufferSource();
    src.buffer = soundBuffer;
    src.connect(audioCtx.destination);
    src.onended = () => { if (soundSource === src) { soundSource = null; soundPlaying = false; } };
    src.start(0);
    soundSource = src;
    soundPlaying = true;
    return;
  }
  // tartalék: sima <audio>
  if (!offlineSound.paused) return;
  offlineSound.currentTime = 0;
  offlineSound.play().catch(() => { pendingOfflineSound = true; });
}

function stopOfflineSound() {
  if (soundSource) {
    soundSource.onended = null;
    try { soundSource.stop(); } catch (e) {}
    soundSource = null;
  }
  soundPlaying = false;
  if (offlineSound) { offlineSound.pause(); offlineSound.currentTime = 0; }
}

// ---------- gif csere ----------
if (offlineGif?.dataset.loading) new Image().src = offlineGif.dataset.loading; // előtöltés

function setOfflineGif(loading) {
  if (!offlineGif) return;
  const url = loading ? offlineGif.dataset.loading : offlineGif.dataset.default;
  if (url && !offlineGif.src.endsWith(url.split("/").pop())) offlineGif.src = url;
}

// projekt betöltésekor másik gif (az inline onclick is lefut mellette)
offlineOpen?.addEventListener("click", () => setOfflineGif(true));

// ---------- offline riasztás ----------
function offlineAlert(offline) {
  if (offline === wasOffline) return;
  wasOffline = offline;
  if (offlineCard) offlineCard.hidden = !offline;
  clearTimeout(offlineDebounce);

  if (!offline) {
    pendingOfflineSound = false;
    stopOfflineSound();
    setOfflineGif(false); // következő offline-nál újra az eredeti gif
    return;
  }

  if (!offlineSound) return;
  // rövid debounce a REAPER-indításkori villogás ellen
  offlineDebounce = setTimeout(() => {
    if (!wasOffline || soundPlaying) return;
    playOfflineSound();
  }, 150);
}

// ---------- hang feloldása (iOS: click/touchend kell, a pointerdown nem elég) ----------
const unlockSound = () => {
  if (audioCtx && audioCtx.state !== "running") audioCtx.resume().catch(() => {});

  if (pendingOfflineSound && wasOffline) {
    pendingOfflineSound = false;
    playOfflineSound();
  }

  // <audio> tartalék feloldása
  if (offlineSound && !soundBuffer) {
    offlineSound.muted = true;
    offlineSound.play().then(() => { offlineSound.pause(); offlineSound.currentTime = 0; offlineSound.muted = false; })
      .catch(() => { offlineSound.muted = false; });
  }

  if (audioCtx && audioCtx.state === "running") {
    ["click", "touchend", "keydown"].forEach(ev => document.removeEventListener(ev, unlockSound));
  }
};
["click", "touchend", "keydown"].forEach(ev => document.addEventListener(ev, unlockSound));

function setOnline(online, message = "") {
  body.classList.toggle("offline", !online);
  syncProjectButtons();
  setFavicon(!online);
  offlineAlert(!online);
  el.connText.textContent = online ? (IS_MOCK ? "Demó mód" : "Status ok") : "Status off";
  el.conn.title = message;
}

/* ---------- level meters ---------- */

const METER_MS = window.matchMedia("(max-width: 520px)").matches ? 150 : 60;
let metersBusy = false;

function levelFraction(db) {
  return Math.min(1, Math.max(0, (db - MIN_DB) / (MAX_DB - MIN_DB)));
}

function ensureMeter(row) {
  let meter = row.querySelector(".meter");
  if (!meter) {
    meter = document.createElement("div");
    meter.className = "meter";
    meter.innerHTML = '<div class="bar"><i></i></div><div class="bar"><i></i></div>';
    row.append(meter);
  }
  return meter;
}

/** `meters` = [{index, levels: [l, r]}]; sávok adat nélkül csendbe mennek. */
function paintMeters(meters) {
  const byIndex = new Map(meters.map((m) => [m.index, m.levels]));
  [...el.tracks.children].forEach((row, i) => {
    const levels = byIndex.get(i) ?? [MIN_DB, MIN_DB];
    ensureMeter(row).querySelectorAll(".bar i").forEach((cover, ch) => {
      cover.style.transform = `scaleX(${1 - levelFraction(levels[ch])})`;
    });
  });
}

function feedLive(meters) {
  if (state.transport.status !== "recording" || state.recStart === null) return;
  const armed = new Set(state.tracks.filter((t) => t.armed).map((t) => t.index));
  let db = MIN_DB;
  for (const m of meters) if (armed.has(m.index)) db = Math.max(db, ...m.levels);
  const i = Math.floor((currentPosition() - state.recStart) / LIVE_STEP);
  if (i < 0) return;
  const v = levelFraction(db), b = state.live.buckets;
  while (b.length <= i) b.push(v);
  b[i] = Math.max(b[i], v);
  state.peaksVersion++; // újrarajzolás
}

async function pollMeters() {
  if (!document.hidden && !metersBusy) {
    metersBusy = true;
    try {
      const meters = await api("GET", "/api/meters");
      paintMeters(meters);
      feedLive(meters);
    } catch {
      paintMeters([]); // offline: minden sáv csendbe
    } finally {
      metersBusy = false;
    }
  }
  setTimeout(pollMeters, METER_MS);
}

/* ---------- tracks ---------- */

function chip(kind, text) {
  const span = document.createElement("span");
  span.className = `chip ${kind}`;
  const label = document.createElement("b");
  label.textContent = kind === "in" ? "BE" : "KI";
  span.append(label, ` ${text}`);
  return span;
}

function setReadout(row, db) {
  row.querySelector(".db").textContent = `${db > 0 ? "+" : ""}${db.toFixed(1)} dB`;
}

function setToggle(button, on) {
  button.classList.toggle("on", on);
  button.setAttribute("aria-pressed", String(on));
}

/** Wire an arm / M / S button: flip it right away, tell REAPER, then re-read the real state. */
function bindToggle(row, index, selector, path) {
  const button = row.querySelector(selector);
  button.addEventListener("click", async () => {
    const on = !button.classList.contains("on");
    setToggle(button, on);
    row.classList.toggle("muted", row.querySelector(".tbtn.m").classList.contains("on"));
    row.classList.toggle("armed", row.querySelector(".tbtn.r").classList.contains("on"));
    row.dataset.toggledAt = performance.now();
    await post(`/api/tracks/${index}/${path}`, { on });
    row.dataset.toggledAt = 0;
    refreshTracks();
  });
}

/** (Re)build the output checkboxes when the list of hardware outputs changed. */
function buildOutputs(row) {
  const grid = row.querySelector(".outs-grid");
  const options = [[MASTER, "Master"], ...state.hwOutputs.map((label) => [label, label])];
  grid.replaceChildren(
    ...options.map(([key, label]) => {
      const wrap = document.createElement("label");
      wrap.className = "opt";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.dataset.out = key;
      wrap.append(input, ` ${label}`);
      return wrap;
    }),
  );
  row.dataset.outsKey = state.hwOutputs.join("|");
}

function createRow(index) {
  const row = document.createElement("li");
  row.className = "track";
  row.dataset.index = index;
  row.innerHTML = `
    <div class="meta">
      <div class="name"></div>
      <div class="routing"></div>
      <label class="in-pick"><span>Bemenet</span><select class="in-select"></select></label>
      <details class="outs">
        <summary>Kimenetek</summary>
        <div class="outs-grid"></div>
      </details>
    </div>
    <div class="vol">
      <div class="ms">
        <button type="button" class="tbtn r" title="Felvételre élesít" aria-label="Felvételre élesít" aria-pressed="false">●</button>
        <button type="button" class="tbtn m" title="Mute" aria-label="Mute" aria-pressed="false">M</button>
        <button type="button" class="tbtn s" title="Solo" aria-label="Solo" aria-pressed="false">S</button>
      </div>
      <input class="fader" type="range" min="${MIN_DB}" max="${MAX_DB}" step="0.5" aria-label="Hangerő">
      <output class="db"></output>
    </div>`;

  // volume
  const fader = row.querySelector(".fader");
  const sendVolume = throttle(
    (db) => api("POST", `/api/tracks/${index}/volume`, { db }).catch((err) => toast(err.message)),
    80,
  );
  fader.addEventListener("input", () => {
    const db = Number(fader.value);
    row.dataset.editedAt = performance.now(); // don't let a poll overwrite what the user is dragging
    paintRange(fader);
    setReadout(row, db);
    sendVolume(db);
  });

  // arm / mute / solo
  bindToggle(row, index, ".tbtn.r", "arm");
  bindToggle(row, index, ".tbtn.m", "mute");
  bindToggle(row, index, ".tbtn.s", "solo");

  const inSel = row.querySelector(".in-select");
  inSel.addEventListener("change", async () => {
    row.dataset.toggledAt = performance.now();
    await post(`/api/tracks/${index}/input`, { value: Number(inSel.value) });
    row.dataset.toggledAt = 0;
    refreshTracks();
  });

  // outputs
  row.querySelector(".outs-grid").addEventListener("change", async (event) => {
    const key = event.target.dataset.out;
    if (!key) return;
    row.dataset.toggledAt = performance.now();
    const on = event.target.checked;
    await (key === MASTER
      ? post(`/api/tracks/${index}/master`, { on })
      : post(`/api/tracks/${index}/hw-output`, { label: key, on }));
    row.dataset.toggledAt = 0;
    refreshTracks();
  });
  return row;
}


/** <option>-ök, a "group" mezős elemek <optgroup>-ba rendezve (mint REAPER-ben a menü). */
function fillInputSelect(select, opts) {
  const groups = new Map();
  const nodes = [];
  for (const o of opts) {
    const option = new Option(o.label, o.value);
    if (!o.group) { nodes.push(option); continue; }
    if (!groups.has(o.group)) {
      const g = document.createElement("optgroup");
      g.label = o.group;
      groups.set(o.group, g);
      nodes.push(g);
    }
    groups.get(o.group).append(option);
  }
  select.replaceChildren(...nodes);
}

/** A bemenet-lista; ha a sáv értéke nincs a listában (pl. ReaRoute), ideiglenes elemként felvesszük. */
function syncInput(row, track) {
  const select = row.querySelector(".in-select");
  const known = state.inputOptions.some((o) => o.value === track.input_value);
  const opts = known ? state.inputOptions : [...state.inputOptions, { value: track.input_value, label: track.input, group: "" }];
  const key = opts.map((o) => o.value).join("|");
  if (row.dataset.insKey !== key) {
    fillInputSelect(select, opts);
    row.dataset.insKey = key;
  }
  if (!recent(row, "toggledAt") && document.activeElement !== select) select.value = String(track.input_value);
}

function updateRow(row, track) {
  row.querySelector(".name").textContent = `${track.index + 1} · ${track.name}`;

  const outputs = [...(track.master_send ? ["Master"] : []), ...track.hw_outputs];
  row.querySelector(".routing").replaceChildren(
    chip("in", track.input),
    ...(outputs.length ? outputs : ["—"]).map((out) => chip("out", out)),
  );

  if (!recent(row, "editedAt")) {
    const fader = row.querySelector(".fader");
    fader.value = track.volume_db;
    paintRange(fader);
    setReadout(row, track.volume_db);
  }

  if (row.dataset.outsKey !== state.hwOutputs.join("|")) buildOutputs(row);

  syncInput(row, track);

  if (!recent(row, "toggledAt")) {
    setToggle(row.querySelector(".tbtn.r"), track.armed);
    setToggle(row.querySelector(".tbtn.m"), track.muted);
    setToggle(row.querySelector(".tbtn.s"), track.solo);
    row.classList.toggle("armed", track.armed);
    row.classList.toggle("muted", track.muted);
    const active = new Set([...(track.master_send ? [MASTER] : []), ...track.hw_outputs]);
    row.querySelectorAll(".outs-grid input").forEach((input) => {
      input.checked = active.has(input.dataset.out);
    });
  }
}

function renderTracks(tracks) {
  state.tracks = tracks;

  while (el.tracks.children.length > tracks.length) el.tracks.lastElementChild.remove();
  tracks.forEach((track, i) => {
    let row = el.tracks.children[i];
    if (!row) {
      row = createRow(i);
      el.tracks.append(row);
    }
    updateRow(row, track);
  });
  renderControls();
}

/* ---------- controls ---------- */

function renderControls() {
  const { status } = state.transport;
  const recording = status === "recording";
  const armed = state.tracks.filter((t) => t.armed);

  body.dataset.status = status;
  el.status.dataset.status = status;
  el.status.textContent = STATUS_LABEL[status];

  setButton(el.rec, "●", recording ? "Felvétel leállítása" : "Felvétel");
  el.rec.classList.toggle("active", recording);
  el.rec.disabled = !recording && !(status === "stopped" && armed.length);
  setButton(el.play, status === "playing" ? "❚❚" : "▶", status === "playing" ? "Szünet" : "Lejátszás");
  el.play.disabled = recording;
  el.stop.disabled = status === "stopped";
  el.start.disabled = recording;
  el.wave.classList.toggle("disabled", recording);
  el.gotoInput.disabled = recording;
  el.gotoBtn.disabled = recording;

  el.hint.textContent = armed.length
    ? `${recording ? "Felvétel folyamatban" : "Armed"}: ${armed.map((t) => `${t.index + 1} · ${t.name}`).join(", ")}`
    : "Élesíts egy vagy több sávot (●) a felvételhez.";

  [...el.tracks.children].forEach((row, i) => {
    row.classList.toggle("recording", recording && Boolean(state.tracks[i]?.armed));
    row.querySelector(".tbtn.r").disabled = recording;
    row.querySelector(".in-select").disabled = recording;
  });

  syncProjectButtons();
  lockTakes();
  syncTakePlay();
  refreshColors();
}

function applyTransport(transport) {
  const wasRecording = state.transport.status === "recording";
  state.transport = transport;
  if (!wasRecording && transport.status === "recording") { state.recStart = transport.position; state.live.buckets = []; }
  state.receivedAt = performance.now();
  renderControls();
  if (wasRecording && transport.status !== "recording") {
    Promise.resolve(loadPeaks()).then(() => { state.live.buckets = []; state.peaksVersion++; });
  }
}

el.rec.addEventListener("click", async () => {
  if (state.transport.status === "recording") {
    await act("/api/transport/stop");
    return;
  }
  const tracks = state.tracks.filter((t) => t.armed).map((t) => t.index);
  if (tracks.length && (await act("/api/record", { tracks }))) refreshTracks();
});
/*el.play.addEventListener("click", () => {
  state.stopAt = null;
  act(`/api/transport/${state.transport.status === "playing" ? "pause" : "play"}`);
});*/
/** A kijelölt take, ha a fej benne van (a végéhez képest 50 ms tűréssel). */
function selectedTakeAt(position) {
  const take = state.takes.find((t) => t.id === state.selectedTakeId);
  return take && position >= take.start - 0.05 && position < take.end - 0.05 ? take : null;
}
el.play.addEventListener("click", async () => {
  state.stopAt = null;
  if (state.transport.status === "playing") {
    act("/api/transport/pause");
    return;
  }
  const take = selectedTakeAt(currentPosition());
  if ((await act("/api/transport/play")) && take) state.stopAt = take.end;
});
el.stop.addEventListener("click", () => act("/api/transport/stop"));
el.start.addEventListener("click", () => {
  if (!state.view.auto) { // nagyított nézetben is látszódjon a lejátszófej
    state.view.start = 0;
    state.view.follow = true;
  }
  commitSeek(0);
});

/* ---------- seeking: typed time ---------- */

function commitSeek(position) {
  state.stopAt = null;
  state.transport = { ...state.transport, position }; // optimistic, avoids a visual jump
  state.receivedAt = performance.now();
  return act("/api/seek", { position });
}

el.gotoInput.addEventListener("focus", () => {
  if (!el.gotoInput.value) el.gotoInput.value = fmt(currentPosition());
  el.gotoInput.select();
});
el.goto.addEventListener("submit", async (event) => {
  event.preventDefault();
  const position = parseTime(el.gotoInput.value);
  if (position === null) {
    toast("Hibás időformátum. Példák: 1:23.5 · 83,5 · 1:02:03");
    return;
  }
  if (await commitSeek(position)) {
    el.gotoInput.value = "";
    el.gotoInput.blur();
  }
});

/* ---------- seeking + zoom: waveform ---------- */

const ctx = el.wave.getContext("2d");
const colors = { played: "", idle: "", head: "", status: "", version: 0 };
const TICK_STEPS = [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
const pointers = new Map(); // active fingers / mouse on the waveform
let dragging = false; // one pointer is scrubbing (seeking)
let pinch = null; // two pointers: zoom + pan
let lastDrawKey = "";
let lastViewKey = "";

function refreshColors() {
  const status = state.transport.status === "recording" ? "recording" : "normal";
  if (colors.status === status && colors.played) return;
  const css = getComputedStyle(document.documentElement);
  colors.played = css.getPropertyValue(status === "recording" ? "--rec" : "--accent").trim();
  colors.idle = css.getPropertyValue("--muted").trim();
  colors.head = css.getPropertyValue("--text").trim();
  colors.status = status;
  colors.version++;
}
window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
  colors.status = "";
  refreshColors();
});

/* --- view window (what part of the timeline is visible) ---
 * auto: 0 -> end of the material plus free room, so you can always seek past the recorded part.
 * Once you zoom or pan it stays where you put it ("Auto" button goes back). */

function limits(real = currentPosition()) {
  const recording = state.transport.status === "recording";
  const material = recording ? real : state.transport.length; // a felvett anyag vége
  const end = Math.max(state.transport.length, real);
  return {
    auto: Math.max(5, material || 60), // 0 -> az anyag vége, az anyag vége = a sáv jobb széle
    maxExtent: Math.max(600, end * 4), // furthest you can zoom out / scroll
  };
}

function currentView(lim) {
  const v = state.view;
  if (v.auto) {
    v.start = 0;
    v.span = lim.auto;
  } else {
    v.span = Math.min(lim.maxExtent, v.span);
    v.start = Math.max(0, Math.min(lim.maxExtent - v.span, v.start));
  }
  return v;
}

function setView(start, span) {
  const lim = limits();
  const v = state.view;
  v.span = Math.min(lim.maxExtent, Math.max(MIN_SPAN, span));
  v.start = Math.max(0, Math.min(lim.maxExtent - v.span, start));
  v.auto = false;
  const real = currentPosition();
  v.follow = real >= v.start && real <= v.start + v.span; // only chase the playhead if it was in view
}

/** Zoom to `span` seconds while the moment `anchor` stays at the same screen fraction `frac`. */
function setViewAnchored(anchor, frac, span) {
  const clamped = Math.min(limits().maxExtent, Math.max(MIN_SPAN, span));
  setView(anchor - frac * clamped, clamped);
}

function zoomBy(factor) {
  const v = state.view;
  const real = currentPosition();
  const anchor = real >= v.start && real <= v.start + v.span ? real : v.start + v.span / 2;
  setViewAnchored(anchor, (anchor - v.start) / v.span, v.span * factor);
}

function resetView() {
  state.view.auto = true;
  state.view.follow = true;
}

function syncViewControls(view, lim) {
  const maxStart = Math.max(0, lim.maxExtent - view.span);
  const key = `${view.start.toFixed(2)}|${view.span.toFixed(2)}|${maxStart.toFixed(1)}`;
  if (key === lastViewKey) return;
  lastViewKey = key;

  el.pan.disabled = maxStart <= 0;
  el.pan.max = maxStart;
  if (performance.now() - state.panAt > 400) el.pan.value = view.start; // don't fight the thumb being dragged
  el.zoomIn.disabled = view.span <= MIN_SPAN * 1.01;
  el.zoomOut.disabled = view.span >= lim.maxExtent * 0.999;

  const f = view.span < 20 ? fmt : fmtShort;
  el.viewInfo.textContent = `${f(view.start)} – ${f(view.start + view.span)}`;
}

/* --- drawing --- */

function drawWave(position, view) {
  const dpr = window.devicePixelRatio || 1;
  const w = el.wave.clientWidth;
  const h = el.wave.clientHeight;
  const { start, span } = view;
  const xOf = (t) => ((t - start) / span) * w;
  const playX = xOf(position);

  const key = [Math.round(playX * dpr), w, h, dpr, state.peaksVersion, colors.version, start.toFixed(3), span.toFixed(3)].join("|");
  if (key === lastDrawKey) return; // nothing visible changed
  lastDrawKey = key;

  if (el.wave.width !== Math.round(w * dpr) || el.wave.height !== Math.round(h * dpr)) {
    el.wave.width = Math.round(w * dpr);
    el.wave.height = Math.round(h * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const ruler = h >= 60 ? 14 : 0; // time scale along the bottom
  const wh = h - ruler;
  const mid = wh / 2;
  const { length, peaks } = state.peaks;

  if (peaks.length && length > 0) {
    // free area after the material: slightly shaded, dashed line where the material ends
    const endX = xOf(length);
    if (endX < w) {
      ctx.globalAlpha = 0.08;
      ctx.fillStyle = colors.idle;
      ctx.fillRect(Math.max(0, endX), 0, w - Math.max(0, endX), wh);
    }
    if (endX >= 0 && endX <= w) {
      ctx.globalAlpha = 0.6;
      ctx.strokeStyle = colors.idle;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(Math.round(endX) + 0.5, 0);
      ctx.lineTo(Math.round(endX) + 0.5, wh);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // only the buckets that are inside the window
    const bucket = length / peaks.length;
    const barW = (bucket / span) * w;
    const first = Math.max(0, Math.floor(start / bucket));
    const last = Math.min(peaks.length, Math.ceil((start + span) / bucket));
    for (let i = first; i < last; i++) {
      const x = xOf(i * bucket);
      const amp = Math.max(1, peaks[i] * (mid - 4));
      const played = x + barW <= playX;
      ctx.globalAlpha = played ? 1 : 0.5;
      ctx.fillStyle = played ? colors.played : colors.idle;
      ctx.fillRect(x, mid - amp, Math.max(barW > 3 ? barW - 1 : barW - 0.4, 0.6), amp * 2);
    }
    ctx.globalAlpha = 1;
  } else {
    ctx.globalAlpha = 0.5;
    ctx.fillStyle = colors.idle;
    ctx.fillRect(0, mid - 0.5, w, 1);
    ctx.globalAlpha = 1;
  }

  const lb = state.live.buckets;
  if (lb.length && state.recStart !== null) {
    const bw = (LIVE_STEP / span) * w;
    const i0 = Math.max(0, Math.floor((start - state.recStart) / LIVE_STEP));
    const i1 = Math.min(lb.length, Math.ceil((start + span - state.recStart) / LIVE_STEP));
    ctx.fillStyle = colors.played;
    ctx.globalAlpha = 1;
    for (let i = i0; i < i1; i++) {
      const amp = Math.max(1, lb[i] * (mid - 4));
      ctx.fillRect(xOf(state.recStart + i * LIVE_STEP), mid - amp, Math.max(bw - 0.4, 1), amp * 2);
    }
  }

  if (ruler) {
    const step = TICK_STEPS.find((s) => (s / span) * w >= 64) || 3600;
    ctx.globalAlpha = 0.85;
    ctx.fillStyle = colors.idle;
    ctx.font = "10px -apple-system, system-ui, sans-serif";
    for (let n = Math.ceil(start / step); n * step <= start + span; n++) {
      const x = xOf(n * step);
      ctx.fillRect(Math.round(x), wh, 1, 4);
      ctx.fillText(step >= 1 ? fmtShort(n * step) : fmt(n * step), x + 3, h - 2);
    }
    ctx.globalAlpha = 1;
  }

  if (playX >= -2 && playX <= w + 2) {
    ctx.fillStyle = colors.head;
    ctx.fillRect(Math.min(w - 2, Math.max(0, playX - 1)), 0, 2, wh);
  }
}

/* --- pointer input: 1 finger = seek, 2 fingers = pinch zoom + pan, wheel = zoom --- */

function waveFrac(clientX) {
  const rect = el.wave.getBoundingClientRect();
  return (clientX - rect.left) / rect.width;
}

function waveTime(clientX) {
  const v = state.view;
  return Math.max(0, v.start + Math.min(1, Math.max(0, waveFrac(clientX))) * v.span);
}

function pinchMetrics() {
  const [a, b] = [...pointers.values()];
  return { dist: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y)), x: (a.x + b.x) / 2 };
}

el.wave.addEventListener("pointerdown", (event) => {
  pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  el.wave.setPointerCapture(event.pointerId);
  if (pointers.size === 1) {
    dragging = state.transport.status !== "recording";
    if (dragging) state.seekPreview = waveTime(event.clientX);
  } else if (pointers.size === 2) {
    dragging = false; // a second finger turns the gesture into zoom, no seek
    state.seekPreview = null;
    const m = pinchMetrics();
    pinch = { dist: m.dist, span: state.view.span, anchor: waveTime(m.x) };
  }
});

el.wave.addEventListener("pointermove", (event) => {
  if (!pointers.has(event.pointerId)) return;
  pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  if (pinch && pointers.size >= 2) {
    const m = pinchMetrics();
    setViewAnchored(pinch.anchor, waveFrac(m.x), (pinch.span * pinch.dist) / m.dist);
  } else if (dragging) {
    state.seekPreview = waveTime(event.clientX);
  }
});

function endPointer(event, commit) {
  if (!pointers.delete(event.pointerId)) return;
  if (pointers.size < 2) pinch = null;
  if (pointers.size > 0) return;
  const seek = dragging && commit;
  dragging = false;
  state.seekPreview = null;
  if (seek) commitSeek(waveTime(event.clientX));
}
el.wave.addEventListener("pointerup", (event) => endPointer(event, true));
el.wave.addEventListener("pointercancel", (event) => endPointer(event, false));

el.wave.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const v = state.view;
    const unit = event.deltaMode === 1 ? 16 : 1;
    if (event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
      const dx = (event.deltaX || event.deltaY) * unit;
      setView(v.start + (dx / el.wave.clientWidth) * v.span, v.span);
    } else {
      const frac = waveFrac(event.clientX);
      setViewAnchored(v.start + frac * v.span, frac, v.span * Math.exp(event.deltaY * unit * 0.002));
    }
  },
  { passive: false },
);

el.wave.addEventListener("keydown", (event) => {
  if (event.key === "+" || event.key === "=") zoomBy(0.5);
  else if (event.key === "-") zoomBy(2);
  else if (event.key === "0") resetView();
  else if (state.transport.status === "recording") return;
  else if (event.key === "ArrowLeft") commitSeek(Math.max(0, currentPosition() - (event.shiftKey ? 10 : 1)));
  else if (event.key === "ArrowRight") commitSeek(currentPosition() + (event.shiftKey ? 10 : 1));
  else return;
  event.preventDefault();
});

el.zoomIn.addEventListener("click", () => zoomBy(0.5));
el.zoomOut.addEventListener("click", () => zoomBy(2));
el.zoomFit.addEventListener("click", resetView);
el.pan.addEventListener("input", () => {
  state.panAt = performance.now();
  setView(Number(el.pan.value), state.view.span);
});

async function loadPeaks() {
  if (state.peaksLoading) return;
  state.peaksLoading = true;
  try {
    const data = await api("GET", `/api/peaks?buckets=${PEAK_BUCKETS}`);
    const sig = `${data.length}:${data.peaks.join(",")}`;
    if (sig !== state.peaksSig) {
      state.peaksSig = sig;
      state.peaks = data;
      state.peaksVersion++;
    }
  } catch {
    // no waveform is not fatal; connection problems are reported by pollTransport
  } finally {
    state.peaksLoading = false;
  }
}

/* ---------- takes ---------- */

const el2 = { favs: $("favs"), favsCard: $("favs-card") };
const takeLists = [el.takes, el2.favs];

const FAV_KEY = "reaper.favOrder";
function favOrder() {
  try { return JSON.parse(localStorage.getItem(FAV_KEY)) || {}; } catch { return {}; }
}
function noteFav(name, on) {
  const o = favOrder();
  if (on) o[name] = Date.now(); else delete o[name];
  try { localStorage.setItem(FAV_KEY, JSON.stringify(o)); } catch { /* privát mód */ }
}
/** Legutóbb csillagozott elöl; időbélyeg nélkülieknél a legújabb felvétel. */
const sortFavs = (list) => {
  const o = favOrder();
  return [...list].sort((a, b) => (o[b.id] || 0) - (o[a.id] || 0) || b.start - a.start);
};

// Contract with the backend:
//   GET  /api/takes          -> [{ name, start, end, tracks: [name...], audible }]
//   POST /api/takes/select   <- { names: [name, name, ...] }  (these become audible, the rest mute)
const TAKES_URL = "/api/takes";
const TAKES_SELECT_URL = "/api/takes/select";

function normalizeTake(raw, i) {
  const start = Number(raw.start ?? raw.position ?? 0);
  const end = Number(raw.end ?? start + Number(raw.length ?? 0));
  const tracks = (raw.tracks ?? raw.track_indices ?? []).map((t) =>
    typeof t === "number" ? state.tracks[t]?.name ?? `Sáv ${t + 1}` : String(t),
  );
  // The take's own name (REAPER's P_NAME on the take) IS the identifier now. The backend
  // saves a "<number>_" prefix into this name the first time it sees an un-numbered take,
  // so the number is part of the name itself — no separate ordinal field needed here.
  const name = raw.name || `Take ${i + 1}`;
  return { id: name, name, start, end, tracks, audible: raw.audible !== false, starred: state.starPending.has(name) ? state.starPending.get(name) : Boolean(raw.starred) };
}

const ICON_PLAY = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M7.5 5v14l12-7z" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M7 5v14M17 5v14" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round"/></svg>';

/** A most lejátszott take ▶ gombja ❚❚ lesz. */
function syncTakePlay() {
  const playing = state.transport.status === "playing" && state.stopAt !== null ? state.selectedTakeId : null;
  el.takes.querySelectorAll(".take").forEach((row) => {
    const b = row.querySelector(".take-btn.play");
    if (!b) return;
    const on = row.dataset.id === playing;
    if (b.dataset.on === String(on)) return;
    b.dataset.on = String(on);
    b.innerHTML = on ? ICON_PAUSE : ICON_PLAY;
    b.title = on ? "Szünet" : "Lejátszás";
  });
}

const ICON_TRASH = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
function takeRow(take) {
  const row = document.createElement("li");
  row.className = `take${take.audible ? "" : " off"}${take.starred ? " starred" : ""}${take.id === state.selectedTakeId ? " selected" : ""}`;
  row.dataset.id = take.id;

  const title = document.createElement("div");
  title.className = "take-title";
  title.textContent = `${take.name} · ${fmt(take.start)} → ${fmt(take.end)}`;
  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = [fmt(Math.max(0, take.end - take.start)), take.tracks.join(", ")].filter(Boolean).join(" · ");
  const info = document.createElement("div");
  info.className = "take-info";
  info.append(title, sub);

  const actions = document.createElement("div");
  actions.className = "take-actions";
  const play = document.createElement("button");
  play.type = "button";
  play.className = "take-btn play";
  play.innerHTML = ICON_PLAY;
  play.title = "Lejátszás";
  play.setAttribute("aria-label", `Lejátszás: ${take.name}`);
  const rename = document.createElement("button");
  rename.type = "button";
  rename.className = "take-btn rename";
  rename.textContent = "✎";
  rename.title = "Átnevezés";
  rename.setAttribute("aria-label", `Átnevezés: ${take.name}`);
  const del = document.createElement("button");
  del.type = "button";
  del.className = "take-btn delete";
  del.innerHTML = ICON_TRASH;
  del.title = "Törlés";
  del.setAttribute("aria-label", `Törlés: ${take.name}`);
  actions.append(play, rename, del);

  row.tabIndex = 0; // billentyűzettel is elérhető
  row.setAttribute("role", "button");
  row.setAttribute("aria-label", `Ugrás ide: ${take.name}`);
  const star = document.createElement("span");
  star.className = "take-star";
  star.textContent = "★";
  star.setAttribute("aria-hidden", "true");
  row.append(info, actions, star);
  return row;
}


function renderTakes(list) {
  if (state.renaming) return; // ne rajzoljuk újra szerkesztés közben
  const takes = list.map(normalizeTake);
  const sig = JSON.stringify(takes);
  if (sig === state.takesSig) return;
  state.takesSig = sig;
  state.takes = takes;
  drawTakes();
}

function drawTakes() {
  const favs = sortFavs(state.takes.filter((t) => t.starred));
  const rest = [...state.takes.filter((t) => !t.starred)].sort((a, b) => b.start - a.start);
  el.takes.replaceChildren(...rest.map(takeRow));
  el2.favs.replaceChildren(...favs.map(takeRow));
  el2.favsCard.hidden = !favs.length;
  lockTakes();
  syncTakePlay();
}

function lockTakes() {
  const recording = state.transport.status === "recording";
  document.querySelectorAll(".take-btn").forEach((b) => { b.disabled = recording; });
}

async function loadTakes() {
  const epoch = state.takesEpoch;
  try {
    const list = await api("GET", TAKES_URL);
    if (epoch === state.takesEpoch) renderTakes(list); // közben módosítottunk valamit: ez a válasz már régi
  } catch (err) {
    // Surfaced instead of swallowed, so a load failure after F5 is visible
    // instead of just "the takes don't show up" with no clue why.
    console.error("Nem sikerült betölteni a take-eket:", err);
  }
}
// Note: pollTracks() below already calls loadTakes() every 2s, so no extra
// setInterval is needed here — one is enough, two would just double the load.

// Sends the *complete* set of take names that should be audible; the server
// decides what "selected" means and we just render whatever it hands back,
// instead of guessing at optimistic UI state that can drift from reality.
// Uses api() (not post()) because post() only resolves to true/false, never
// the actual JSON body — that mismatch was the "list.map is not a function" bug.
async function selectAudible(names) {
  const updated = await api("POST", TAKES_SELECT_URL, { names });
  if (!Array.isArray(updated)) {
    console.error("Váratlan válasz a /api/takes/select hívásra:", updated);
    return loadTakes();
  }
  state.takesSig = ""; // force a redraw even if the shape happens to match
  renderTakes(updated);
}

let takeDragMoved = false; // egérrel húztuk a listát, ne számítson kattintásnak

let lastTap = { id: null, at: 0 };
const DOUBLE_TAP_MS = 350;

async function toggleStar(take, row) {
  const starred = !take.starred;
  take.starred = starred;
  noteFav(take.id, starred);
  drawTakes(); // azonnal átugrik a másik listába, nem vár a szerverre
  state.starPending.set(take.id, starred); // amíg a kérés fut, minden újrarajzolás ezt mutatja (különben villan)
  state.takesEpoch++;
  /* row.classList.toggle("starred", starred); */
  try {
    const updated = await api("POST", "/api/takes/star", { name: take.id, starred });
    state.starPending.delete(take.id);
    state.takesEpoch++;
    state.takesSig = "";
    if (Array.isArray(updated)) renderTakes(updated);
    else await loadTakes();
  } catch (err) {
    state.starPending.delete(take.id);
    state.takesEpoch++;
    state.takesSig = "";
    await loadTakes(); // hiba: vissza a szerver állapotára
    toast(err.message);
  }
}

const SKIP_CONFIRM_KEY = "reaper.skipDeleteConfirm";

function skipConfirm() {
  try { return localStorage.getItem(SKIP_CONFIRM_KEY) === "1"; } catch { return false; }
}
function rememberSkipConfirm() {
  try { localStorage.setItem(SKIP_CONFIRM_KEY, "1"); } catch { /* privát mód: nem megjegyezhető */ }
}

/** Megerősítő ablak. Visszaad: { ok, skip } */
function askDeleteConfirm(name) {
  return new Promise((resolve) => {
    el.confirmName.textContent = name;
    el.confirmSkip.checked = false;
    el.confirm.hidden = false;
    el.confirmNo.focus(); // alapból a "Nem" van fókuszban, véletlenül ne törölj

    const close = (ok) => {
      const skip = ok && el.confirmSkip.checked; // "Nem"-nél a pipa nem számít
      el.confirm.hidden = true;
      el.confirmYes.removeEventListener("click", onYes);
      el.confirmNo.removeEventListener("click", onNo);
      el.confirm.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKey);
      resolve({ ok, skip });
    };
    const onYes = () => close(true);
    const onNo = () => close(false);
    const onBackdrop = (event) => { if (event.target === el.confirm) close(false); };
    const onKey = (event) => { if (event.key === "Escape") close(false); };

    el.confirmYes.addEventListener("click", onYes);
    el.confirmNo.addEventListener("click", onNo);
    el.confirm.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKey);
  });
}

async function deleteTake(take) {
  if (!skipConfirm()) {
    const { ok, skip } = await askDeleteConfirm(`${take.name} · ${fmt(take.start)} → ${fmt(take.end)}`);
    if (!ok) return;
    if (skip) rememberSkipConfirm();
  }
  state.takesBusy++;
  state.takesEpoch++;
  try {
    const updated = await api("POST", "/api/takes/delete", { name: take.id });
    if (state.selectedTakeId === take.id) state.selectedTakeId = null;
    state.starPending.delete(take.id);
    state.takesEpoch++;
    state.takesSig = "";
    if (Array.isArray(updated)) renderTakes(updated);
    else await loadTakes();
  } catch (err) {
    state.takesEpoch++;
    toast(err.message);
  } finally {
    state.takesBusy--;
  }
  loadPeaks(); // a hullámforma is frissüljön
}

const AUDIO_EXT = /\.(wav|wave|flac|mp3|aiff?|ogg)$/i;
function renameTake(take, row) {
  if (state.renaming) return;
  const title = row.querySelector(".take-title");
  if (!title) return;

  state.renaming = true;
  const original = title.textContent;
  const ext = (take.name.match(AUDIO_EXT) || [""])[0]; // pl. ".wav" vagy ""
  const base = ext ? take.name.slice(0, -ext.length) : take.name;

  const input = document.createElement("input");
  input.type = "text";
  input.className = "take-rename-input";
  input.value = base;
  input.autocomplete = "off";
  input.spellcheck = false;
  input.enterKeyHint = "done";
  input.setAttribute("aria-label", "Take új neve");

  const wrap = document.createElement("span");
  wrap.className = "take-rename";
  wrap.append(input);
  if (ext) {
    const extLabel = document.createElement("span");
    extLabel.className = "take-rename-ext";
    extLabel.textContent = ext;
    wrap.append(extLabel);
  }
  title.replaceChildren(wrap);

  // fókusz + kijelölés még a koppintás eseményén belül (iOS-en csak így jön fel a billentyűzet)
  input.focus();
  input.setSelectionRange(0, input.value.length);
  input.select();
  setTimeout(() => input.select(), 50); // iOS néha elveszti a kijelölést

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    state.renaming = false;
    let typed = input.value.trim();
    if (ext && typed.toLowerCase().endsWith(ext.toLowerCase())) typed = typed.slice(0, -ext.length).trim();
    const newName = typed ? typed + ext : "";
    if (!save || !newName || newName === take.name) {
      title.textContent = original;
      return;
    }
    title.textContent = `${newName} · …`; // amíg a szerver válaszol
    state.takesEpoch++;
    try {
      const { name, takes } = await api("POST", "/api/takes/rename", { name: take.id, new_name: newName });
      if (state.selectedTakeId === take.id) state.selectedTakeId = name;
      state.takesEpoch++;
      state.takesSig = "";
      renderTakes(takes);
    } catch (err) {
      state.takesEpoch++;
      title.textContent = original;
      toast(err.message);
    }
  };

  input.addEventListener("keydown", (event) => {
    event.stopPropagation();
    if (event.key === "Enter") { event.preventDefault(); finish(true); }
    else if (event.key === "Escape") { event.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

const onTakeClick = async (event) => {
  if (event.target.closest(".take-rename-input")) return;
  if (takeDragMoved) {
    takeDragMoved = false;
    return;
  }
  const row = event.target.closest(".take");
  if (!row || state.transport.status === "recording") return;
  const take = state.takes.find((t) => String(t.id) === row.dataset.id);
  if (!take) return;
  if (event.target.closest(".take-btn.rename")) {
    renameTake(take, row);
    return;
  }
  if (event.target.closest(".take-btn.delete")) {
    deleteTake(take);
    return;
  }

  // kártya = ugrás, ▶ gomb = ugrás + lejátszás
    // dupla koppintás a kártyán (nem a ▶ gombon) = csillag
  const onButton = Boolean(event.target.closest(".take-btn"));
  const now = performance.now();
  const isDouble = !onButton && lastTap.id === take.id && now - lastTap.at < DOUBLE_TAP_MS;
  lastTap = { id: take.id, at: isDouble ? 0 : now };
  if (isDouble) {
    toggleStar(take, row);
    return;
  }
  const wantsPlay = Boolean(event.target.closest(".take-btn.play"));

  if (wantsPlay && state.selectedTakeId === take.id) {
    const st = state.transport.status;
    if (st === "playing" && state.stopAt !== null) { act("/api/transport/pause"); return; }
    if (st === "paused" && selectedTakeAt(currentPosition())) { // folytatás ugyanonnan
      if (await act("/api/transport/play")) state.stopAt = take.end;
      return;
    }
  }

  state.selectedTakeId = take.id; // optimistic highlight while the request is in flight
  document.querySelectorAll(".take.selected").forEach((r) => r.classList.remove("selected"));
  row.classList.add("selected");

  state.takesBusy++;
  try {
    // Mindkettő soloza ezt a take-et: ez lesz az egyetlen, ami szól.
    const [seeked] = await Promise.all([commitSeek(take.start), selectAudible([take.id])]);
    if (seeked && wantsPlay && (await act("/api/transport/play"))) state.stopAt = take.end;
  } catch (err) {
    console.error("Nem sikerült kiválasztani a take-et:", err);
  } finally {
    state.takesBusy--;
  }
  loadPeaks(); // the waveform only shows audible material
};
takeLists.forEach((l) => l.addEventListener("click", onTakeClick));

takeLists.forEach((l) => l.addEventListener("keydown", (event) => {
  if ((event.key === "Enter" || event.key === " ") && event.target.classList.contains("take")) {
    event.preventDefault();
    event.target.click();
  }
}));

/* take-ek: egérrel húzható görgetés (érintésnél a böngésző intézi) */
function enableTakeDrag(list) {
  let drag = null;
  list.addEventListener("pointerdown", (event) => {
    if (event.pointerType !== "mouse" || event.target.closest("button, input")) return;
    drag = { x: event.clientX, left: list.scrollLeft };
    takeDragMoved = false;
  });
  list.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.x;
    if (Math.abs(dx) < 5 && !takeDragMoved) return; // kis rezgés még kattintás
    takeDragMoved = true;
    list.classList.add("dragging");
    list.scrollLeft = drag.left - dx;
  });
  const end = () => { drag = null; list.classList.remove("dragging"); };
  list.addEventListener("pointerup", end);
  list.addEventListener("pointerleave", end);
}
takeLists.forEach(enableTakeDrag);

/* ---------- clock ---------- */

function currentPosition() {
  const { status, position } = state.transport;
  const running = status === "playing" || status === "recording";
  return position + (running ? (performance.now() - state.receivedAt) / 1000 : 0);
}

let lastNow = null;

/** Annak a take-nek a neve, amit épp hallgatsz (vagy amit épp felveszel). */
function nowPlayingName(position) {
  if (state.transport.status === "recording") {
    const mine = state.takes.filter((t) => state.recStart !== null && Math.abs(t.start - state.recStart) < 1);
    return mine.length ? mine[mine.length - 1].name : "Felvétel…";
  }
  let best = null;
  for (const t of state.takes) {
    if (t.audible && position >= t.start && position <= t.end && (!best || t.start >= best.start)) best = t;
  }
  return best ? best.name : "";
}

function frame() {
  const real = currentPosition();
  if (state.stopAt !== null) {
    if (state.transport.status === "stopped") {
      state.stopAt = null; // megállították
    } else if (state.transport.status === "playing" && real >= state.stopAt) {
      const end = state.stopAt;
      state.stopAt = null;
      act("/api/transport/stop").then(() => commitSeek(end)); // a fej a take végén marad
    }
  }
  
  const shown = state.seekPreview ?? real;
  const length = Math.max(state.transport.length, real);
  const lim = limits(real);
  const view = currentView(lim);

  // page-flip when the playhead runs out of a zoomed view (unless the user is touching it)
  const running = state.transport.status === "playing" || state.transport.status === "recording";
  const touching = pointers.size > 0 || performance.now() - state.panAt < 400;
  if (!view.auto && running && view.follow && !touching && (real < view.start || real > view.start + view.span)) {
    view.start = Math.max(0, Math.min(lim.maxExtent - view.span, real - view.span * 0.1));
  }

  el.time.textContent = fmt(shown);
  const nowName = nowPlayingName(shown);
  if (nowName !== lastNow) {
    lastNow = nowName;
    el.now.textContent = nowName;
    el.now.title = nowName;
  }
  el.total.textContent = fmt(length);
  drawWave(shown, view);
  syncViewControls(view, lim);
  requestAnimationFrame(frame);
}

/* ---------- projektek: mentés / megnyitás / váltás ---------- */

const showModal = (node, on) => { node.style.display = on ? "grid" : "none"; };
const currentProject = () => state.projects.find((p) => p.current);
const projectDir = () => {
  const dir = (currentProject()?.path || "").replace(/[\\/][^\\/]*$/, "");
  return dir.endsWith(":") ? dir + "\\" : dir; // "C:" -> "C:\"
};

class DirBrowser {
  constructor(crumbEl, listEl, { ext = "", onFile = null } = {}) {
    Object.assign(this, { crumbEl, listEl, ext, onFile, path: "" });
  }

  async load(path = "") {
    try {
      const qs = `path=${encodeURIComponent(path)}${this.ext ? `&ext=${encodeURIComponent(this.ext)}` : ""}`;
      const data = await api("GET", `/api/filesystem/dirs?${qs}`);
      this.path = data.path;
      this.renderCrumbs(data.path);
      this.renderList(data);
    } catch (err) {
      toast(err.message);
      if (path) this.load(""); // eltűnt mappa: vissza a meghajtókhoz
    }
  }

  renderCrumbs(path) {
    const parts = path.split(/[\\/]/).filter(Boolean);
    const sep = path.includes("\\") ? "\\" : "/";
    const crumbs = [this._crumb("Meghajtók", "")];
    let acc = "";
    parts.forEach((part, i) => {
      acc = i === 0 ? (sep === "\\" ? part + "\\" : "/" + part) : acc.replace(/[\\/]$/, "") + sep + part;
      crumbs.push(" / ", this._crumb(part, acc));
    });
    this.crumbEl.replaceChildren(...crumbs);
  }

  renderList({ parent, dirs, files = [] }) {
    const rows = [];
    if (parent) rows.push(this._row("⬆ .. (szülő mappa)", () => this.load(parent), "parent"));
    dirs.forEach((d) => rows.push(this._row(`📁 ${d.name}`, () => this.load(d.path))));
    files.forEach((f) => rows.push(this._row(`📄 ${f.name}`, () => this.onFile?.(f.path))));
    this.listEl.replaceChildren(...rows);
  }

  _crumb(text, target) {
    const span = document.createElement("span");
    span.textContent = text;
    span.onclick = () => this.load(target);
    return span;
  }

  _row(text, onClick, cls = "") {
    const div = document.createElement("div");
    div.className = `dir-item ${cls}`.trim();
    div.textContent = text;
    div.onclick = onClick;
    return div;
  }
}

function hideToast() {
  clearTimeout(toastTimer);
  el.toast.classList.remove("show");
}

/* --- mentés --- */
const spOverlay = $("save-project-overlay");
const spName = $("sp-name");
const spConfirm = $("sp-confirm");
const spSubdir = $("sp-create-subdir");
const spMove = $("sp-move-media");
const saveBrowser = new DirBrowser($("sp-breadcrumb"), $("sp-dir-list"), {
  ext: ".rpp",
  onFile: (path) => {
    const base = path.replace(/^.*[\\/]/, "").replace(/\.rpp$/i, "");
    spName.value = base;
    $("sp-current").textContent = `Felülírás: ${path}`;
  },
});


function openSaveProject() {
  if (state.transport.status === "recording") return toast("Felvétel közben nem lehet menteni.");
  const cur = currentProject();
  const saved = Boolean(cur?.path);
  spName.value = saved ? cur.name : "";
  spSubdir.checked = !saved;
  spMove.checked = !saved;
  spConfirm.disabled = false;
  $("sp-current").textContent = saved ? `Jelenlegi: ${cur.path}` : "";
  showModal(spOverlay, true);
  saveBrowser.load(projectDir());
}
const closeSaveProject = () => { showModal(spOverlay, false); hideToast(); };
function toggleSaveProject() {
  if (spOverlay.style.display === "grid") closeSaveProject();
  else openSaveProject();
}
spOverlay.addEventListener("click", (e) => { if (e.target === spOverlay) closeSaveProject(); });
$("sp-cancel").onclick = closeSaveProject;

spConfirm.onclick = async () => {
  const name = spName.value.trim();
  if (!name) return toast("Adj nevet a projektnek.");
  if (!saveBrowser.path) return toast("Válassz mappát.");
  spConfirm.disabled = true;
  try {
    const data = await api("POST", "/api/project/save", {
      directory: saveBrowser.path,
      name,
      create_subdirectory: spSubdir.checked,
      move_media: spMove.checked,
    });
    closeSaveProject();
    toast(`✔ Sikeres mentés: ${data.path}`, "ok");
    loadProjects();
  } catch (err) {
    spConfirm.disabled = false;
    toast(`✖ Mentés sikertelen: ${err.message}`);
  }
};

/* --- megnyitás --- */
const opOverlay = $("open-project-overlay");
const openBrowser = new DirBrowser($("op-breadcrumb"), $("op-dir-list"), { ext: ".rpp", onFile: openProjectFile });
const closeOpenProject = () => showModal(opOverlay, false);

const opTabs = document.querySelectorAll("#open-project-overlay .seg-btn");
const opPanes = { recent: $("op-recent"), browse: $("op-browse"), templates: $("op-templates") };

function showOpenTab(tab) {
  opTabs.forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  Object.entries(opPanes).forEach(([key, pane]) => { pane.hidden = key !== tab; });
  if (tab === "recent") loadRecent();
  else if (tab === "browse") openBrowser.load(projectDir());
  else loadTemplates();
}
opTabs.forEach((b) => b.addEventListener("click", () => showOpenTab(b.dataset.tab)));

function openOpenProject() {
  showModal(opOverlay, true);
  showOpenTab("recent");
}
$("op-cancel").onclick = closeOpenProject;
opOverlay.addEventListener("click", (e) => { if (e.target === opOverlay) closeOpenProject(); });

/** Név + (opcionális) mappa soronként; kattintásra onPick. */
function fillPickList(listEl, items, onPick, emptyText) {
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "dir-item parent";
    empty.textContent = emptyText;
    listEl.replaceChildren(empty);
    return;
  }
  listEl.replaceChildren(
    ...items.map((item) => {
      const row = document.createElement("div");
      row.className = "dir-item pick";
      row.title = item.path;
      const name = document.createElement("span");
      name.className = "pick-name";
      name.textContent = item.name;
      row.append(name);
      if (item.dir) {
        const dir = document.createElement("span");
        dir.className = "pick-sub sub";
        dir.textContent = item.dir;
        row.append(dir);
      }
      row.onclick = () => onPick(item);
      return row;
    }),
  );
}

async function loadRecent() {
  try {
    const list = await api("GET", "/api/projects/recent");
    fillPickList($("op-recent-list"), list, (p) => openProjectFile(p.path), "Nincs legutóbbi projekt.");
  } catch (err) {
    toast(err.message);
  }
}

async function loadTemplates() {
  try {
    const list = await api("GET", "/api/projects/templates");
    fillPickList($("op-templates-list"), list, newFromTemplate, "Nincs sablon a ProjectTemplates mappában.");
  } catch (err) {
    toast(err.message);
  }
}

async function newFromTemplate(template) {
  if (body.classList.contains("offline")) {
    toast("Sablonból új projekthez előbb indítsd el a REAPER-t.");
    return;
  }
  closeOpenProject();
  toast("Új projekt a sablonból…", "ok");
  try {
    renderProjects(await api("POST", "/api/projects/new-from-template", { path: template.path }));
    afterProjectSwitch();
    toast(`✔ Új projekt: ${template.name}`, "ok");
  } catch (err) {
    toast(`✖ ${err.message}`);
  }
}

async function openProjectFile(path) {
  closeOpenProject();
  if (body.classList.contains("offline")) {
    toast("REAPER indítása a kiválasztott projekttel…", "ok");
    try {
      await api("POST", "/api/reaper/launch", { path });
    } catch (err) {
      toast(err.message);
    }
    return; // amint a REAPER felél, pollTransport/pollTracks úgyis frissíti a felületet
  }
  toast("Megnyitás…", "ok");
  try {
    renderProjects(await api("POST", "/api/projects/open", { path }));
    afterProjectSwitch();
    toast("✔ Projekt megnyitva", "ok");
  } catch (err) {
    toast(`✖ Megnyitás sikertelen: ${err.message}`);
  }
}

/* --- hamburger: megnyitott projektek --- */
const projMenu = $("proj-menu");
const projList = $("proj-list");
const projName = $("proj-name");

function renderProjects(list) {
  const sig = JSON.stringify(list);
  if (sig === state.projectsSig) return; // ne villogjon a lista a pollingtól
  state.projectsSig = sig;
  state.projects = list;

  const cur = list.find((p) => p.current);
  projName.textContent = cur ? cur.name : "";
  projName.title = cur?.path || "";
  projName.hidden = !cur;
  document.title = cur ? `${cur.name} · Reaper Studio Magic` : "Reaper Studio Magic";

  projMenu.hidden = list.length < 2;
  if (list.length < 2) projList.hidden = true;
  projList.replaceChildren(
    ...list.map((p) => {
      const li = document.createElement("li");
      li.textContent = p.name;
      li.title = p.path;
      li.className = p.current ? "current" : "";
      li.onclick = () => selectProject(p.index);
      return li;
    }),
  );
}

function afterProjectSwitch() {
  Object.assign(state, { selectedTakeId: null, stopAt: null, takesSig: "", peaksSig: "" });
  refreshTracks();
  loadTakes();
  loadPeaks();
}

async function selectProject(index) {
  projList.hidden = true;
  if (state.projects[index]?.current) return;
  try {
    renderProjects(await api("POST", "/api/projects/select", { index }));
    afterProjectSwitch();
  } catch (err) {
    toast(`✖ ${err.message}`);
  }
}

async function loadProjects() {
  try {
    renderProjects(await api("GET", "/api/projects"));
  } catch {
    // offline: pollTransport jelzi
  }
}

$("proj-btn").addEventListener("click", (e) => { e.stopPropagation(); projList.hidden = !projList.hidden; });
document.addEventListener("click", () => { projList.hidden = true; });


/* ---------- REAPER saját ablakai (hiba / kérdés) ---------- */

const ddBox = $("daw-dialog");
let ddSig = "";

function renderDawDialog(d) {
  const sig = d ? JSON.stringify(d) : "";
  if (sig === ddSig) return;
  ddSig = sig;
  ddBox.hidden = !d;
  if (!d) return;
  $("dd-title").textContent = d.title || "REAPER";
  $("dd-text").textContent = d.text;
  $("dd-actions").replaceChildren(
    ...d.buttons.map((b, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `btn${i === 0 ? " primary" : ""}`;
      btn.textContent = b.label;
      btn.onclick = async () => {
        try {
          await api("POST", "/api/dialogs/click", { hwnd: d.hwnd, button: b.id });
        } catch (err) {
          toast(err.message);
        }
      };
      return btn;
    }),
  );
}

async function pollDialogs() {
  try {
    renderDawDialog((await api("GET", "/api/dialogs"))[0]);
  } catch {
    // a szerver nem elérhető: pollTransport jelzi
  }
  setTimeout(pollDialogs, 1000);
}


/* ---------- belépés ---------- */

const loginBox = $("login-overlay");
const loginForm = $("login-form");
const lgUser = $("lg-user");
const lgPass = $("lg-pass");
const lgErr = $("lg-err");

function showLogin() {
  $("btn-logout").hidden = true;
  if (!loginBox.hidden) return;
  lgErr.textContent = "";
  lgPass.value = "";
  loginBox.hidden = false;
  lgUser.focus();
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  lgErr.textContent = "";
  try {
    const user = await api("POST", "/api/login", { username: lgUser.value, password: lgPass.value });
    loginBox.hidden = true;
    lgPass.value = "";
    $("btn-logout").hidden = false;
    toast(`${user.name} bazdmeg csak beléptél lol`, "ok");
    refreshTracks();
    loadTakes();
    loadPeaks();
    loadProjects();
  } catch (err) {
    lgErr.textContent = err.message;
    lgPass.select();
  }
});

async function doLogout() {
  try { await api("POST", "/api/logout"); } catch { /* mindegy */ }
  showLogin();
}

(async () => {
  try {
    const { user } = await api("GET", "/api/me");
    if (user) $("btn-logout").hidden = false;
    else showLogin();
  } catch { /* offline: a pollTransport jelzi */ }
})();


/* ---------- polling ---------- */


async function pollTransport() {
  let transport;
  try {
    transport = await api("GET", "/api/transport");
  } catch (err) {
    if (err.status === 401) { // nincs belépve: ne legyen offline kártya
      setTimeout(pollTransport, 1000);
      return;
    }
    setOnline(false, err.message);
    setTimeout(pollTransport, 250);
    return;
  }
  setOnline(true); // a hívás sikerült, ez a valódi állapot — a renderelés hibája ezt már nem írhatja felül
  try {
    applyTransport(transport);
  } catch (err) {
    console.error("applyTransport hiba:", err); // ne dobja vissza offline-ba, csak logoljuk
  }
  setTimeout(pollTransport, 250);
}

async function refreshTracks() {
  try {
    renderTracks(await api("GET", "/api/tracks"));
  } catch {
    // connection problems are reported by pollTransport
  }
}

async function loadInputOptions() {
  try {
    state.inputOptions = await api("GET", "/api/input-options");
  } catch {
    // a következő pollnál újra
  }
}

async function loadHwOutputs() {
  try {
    state.hwOutputs = await api("GET", "/api/hw-outputs");
  } catch {
    // retried on the next poll
  }
}

async function pollTracks() {
  if (!state.hwOutputs.length) await loadHwOutputs();
  if (!state.inputOptions.length) await loadInputOptions();
  await refreshTracks();
  loadTakes();
  loadProjects(); // hamburger + aktuális projekt neve, REAPER-ben történt váltásokat is követi
  if (state.transport.status === "stopped") loadPeaks();
  setTimeout(pollTracks, 2000);
}

renderControls();
requestAnimationFrame(frame);
pollTransport();
pollTracks();
pollMeters();
pollDialogs();
