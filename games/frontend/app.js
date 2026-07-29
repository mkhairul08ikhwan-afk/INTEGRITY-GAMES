const API = {
  public: "/api/public",
  stream: "/api/stream",
  leaderboard: "/api/leaderboard",
  selectGames: "/api/coach/select-games",
  register: "/api/register",
  start: "/api/coach/start",
  shutdown: "/api/coach/shutdown",
  scan: "/api/team/scan",
  decodeQr: "/api/qr/decode",
  team: (id) => `/api/team/${encodeURIComponent(id)}`,
};

const STORAGE = {
  teamId: "ih_teamId",
  coachEntered: "ih_coach_entered",
  serverInstanceId: "ih_server_instance_id",
  currentEventId: "ih_current_event_id",
  notice: "ih_notice",
};

let publicState = null;
let leaderboard = { rows: [] };
let sse = null;
let pollTimer = 0;
let pollBusy = false;
let lastToastAt = 0;
let activeScannerStop = null;
let renderSequence = 0;
const ASSET_VERSION = "20260729r3";

function reportDebug() {}

function qs(sel, root = document) {
  return root.querySelector(sel);
}

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

function clamp(n, a, b) {
  return Math.max(a, Math.min(b, n));
}

function escapeHtml(str) {
  return String(str)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return "—";
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (x) => String(x).padStart(2, "0");
  if (h > 0) return `${h}:${pad(m)}:${pad(s)}`;
  return `${m}:${pad(s)}`;
}

function toast(message, kind = "good") {
  const now = Date.now();
  if (now - lastToastAt < 300) return;
  lastToastAt = now;
  const node = el(`<div class="hint ${kind === "bad" ? "bad" : "good"}" role="status"></div>`);
  node.textContent = message;
  const host = qs("#toast-host") || (() => {
    const h = el('<div id="toast-host" style="position:fixed;left:18px;right:18px;bottom:18px;z-index:50;display:grid;gap:10px;max-width:var(--max);margin:0 auto;"></div>');
    document.body.appendChild(h);
    return h;
  })();
  host.appendChild(node);
  setTimeout(() => node.remove(), 2600);
}

function queueNotice(message) {
  if (!message) return;
  sessionStorage.setItem(STORAGE.notice, message);
}

function flushNotice() {
  const message = sessionStorage.getItem(STORAGE.notice);
  if (!message) return;
  sessionStorage.removeItem(STORAGE.notice);
  toast(message, "bad");
}

async function fetchJson(url, options) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const apiError = data?.error || {};
    const msg = apiError.message || data?.description || data?.message || `${res.status} ${res.statusText}`;
    const err = new Error(msg);
    err.status = res.status;
    err.data = data;
    err.code = apiError.code || null;
    throw err;
  }
  return data;
}

function setPath(path) {
  history.pushState({}, "", path);
  render();
}

window.addEventListener("popstate", () => render());

function baseUrl() {
  return `${location.origin}`;
}

function joinUrl() {
  return `${baseUrl()}/join`;
}

function assetUrl(path) {
  return `/assets/${path}?v=${encodeURIComponent(ASSET_VERSION)}`;
}

function gamePayload(gameId) {
  return `IH|GAME|${gameId}`;
}

function renderQrNode(host, text, size = 280, fallbackMessage = "QR generation unavailable") {
  if (!host || typeof window.QRCode !== "function") {
    if (host) host.replaceWith(el(`<div class="hint bad">${escapeHtml(fallbackMessage)}</div>`));
    return false;
  }
  try {
    host.innerHTML = "";
    new window.QRCode(host, {
      text: String(text || ""),
      width: size,
      height: size,
      correctLevel: window.QRCode.CorrectLevel.H,
    });
    const qrImg = host.querySelector("img");
    if (qrImg) {
      qrImg.decoding = "sync";
      qrImg.loading = "eager";
      qrImg.alt = "QR code";
    }
    return true;
  } catch (e) {
    reportDebug("QR", "QR render failed", { text, size, message: e?.message || "unknown" });
    host.replaceWith(el(`<div class="hint bad">${escapeHtml(fallbackMessage)}</div>`));
    return false;
  }
}

function phaseLabel(phase) {
  if (phase === "config") return "Config";
  if (phase === "registration") return "Registration";
  if (phase === "live") return "Live";
  return String(phase || "—");
}

function cleanupActiveScanner() {
  if (typeof activeScannerStop === "function") {
    activeScannerStop();
  }
  activeScannerStop = null;
}

function applyPublicState(nextState) {
  const previousState = publicState;
  const previousServerId = sessionStorage.getItem(STORAGE.serverInstanceId);
  const previousEventId = sessionStorage.getItem(STORAGE.currentEventId);
  const nextServerId = nextState?.serverInstanceId || "";
  const nextEventId = nextState?.currentEventId || "";
  const hasLocalSession = Boolean(getStoredTeamId() || coachEntered());

  if (previousServerId && nextServerId && previousServerId !== nextServerId && hasLocalSession) {
    clearStoredTeamId();
    sessionStorage.removeItem(STORAGE.coachEntered);
    queueNotice("The Render server restarted. Runtime event data was cleared.");
  } else if (
    hasLocalSession &&
    previousEventId &&
    previousEventId !== nextEventId &&
    !nextEventId &&
    previousState &&
    (previousState.phase === "registration" || previousState.phase === "live")
  ) {
    clearStoredTeamId();
    queueNotice("The event has ended and all runtime data was cleared.");
  }

  if (nextServerId) {
    sessionStorage.setItem(STORAGE.serverInstanceId, nextServerId);
  }
  sessionStorage.setItem(STORAGE.currentEventId, nextEventId);
  publicState = nextState;
}

function renderShell(contentEl) {
  const phase = publicState?.phase || "—";
  const root = el(`
    <div class="shell">
      <div class="topbar">
        <div class="topbar-inner">
          <a class="brand" href="/coach">
            <img class="brand-mark" src="${assetUrl("logo.svg")}" alt="CJM Global Integrity Hub logo" />
            <div class="brand-title">
              <strong>Integrity Hunting</strong>
              <span>CJM Global Integrity Hub</span>
            </div>
          </a>
          <div class="pill" title="System phase">
            <span class="dot"></span>
            <span class="mono">${escapeHtml(phaseLabel(phase))}</span>
          </div>
        </div>
      </div>
      <div class="content"></div>
    </div>
  `);
  qs(".content", root).appendChild(contentEl);
  // #region debug-point E:logo-shell
  const brandMark = qs(".brand-mark", root);
  if (brandMark) {
    const emit = () =>
      reportDebug("E", "Brand logo measured in shell", {
        currentSrc: brandMark.currentSrc || brandMark.getAttribute("src") || "",
        naturalWidth: brandMark.naturalWidth || 0,
        naturalHeight: brandMark.naturalHeight || 0,
        clientWidth: brandMark.clientWidth || 0,
        clientHeight: brandMark.clientHeight || 0,
      });
    brandMark.addEventListener("load", emit, { once: true });
    if (brandMark.complete) emit();
  }
  // #endregion
  return root;
}

function viewCard(title, subtitle, bodyHtml) {
  const node = el(`
    <div class="card">
      <div class="card-inner">
        <div class="card-header">
          <div>
            <h1 class="h1">${escapeHtml(title)}</h1>
            <p class="sub">${escapeHtml(subtitle || "")}</p>
          </div>
        </div>
        <div class="grid">${bodyHtml || ""}</div>
      </div>
    </div>
  `);
  return node;
}

async function ensurePublicState() {
  if (publicState) return;
  applyPublicState(await fetchJson(API.public));
}

function connectStream() {
  if (sse) return;
  sse = new EventSource(API.stream);
  sse.addEventListener("state", (ev) => {
    try {
      applyPublicState(JSON.parse(ev.data));
    } catch {
      return;
    }
    render();
  });
  sse.addEventListener("leaderboard", (ev) => {
    try {
      leaderboard = JSON.parse(ev.data);
    } catch {
      return;
    }
    renderLeaderboardIfVisible();
  });
  sse.addEventListener("error", () => {
    sse?.close();
    sse = null;
    setTimeout(connectStream, 1200);
  });
}

async function pollRealtime() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    const nextState = await fetchJson(API.public);
    const prevSerialized = JSON.stringify(publicState || {});
    applyPublicState(nextState);
    const nextSerialized = JSON.stringify(publicState || {});
    if (publicState?.phase === "live") {
      leaderboard = await fetchJson(API.leaderboard);
      renderLeaderboardIfVisible();
    }
    if (prevSerialized !== nextSerialized) {
      render();
    }
  } catch {
    return;
  } finally {
    pollBusy = false;
  }
}

function ensureRealtime() {
  connectStream();
  if (!pollTimer) {
    pollTimer = window.setInterval(() => {
      pollRealtime().catch(() => {});
    }, 2000);
  }
}

function renderLeaderboardIfVisible() {
  const table = qs('[data-role="leaderboard-table"]');
  if (!table) return;
  const tbody = qs("tbody", table);
  if (!tbody) return;
  const rows = leaderboard?.rows || [];
  tbody.innerHTML = rows
    .map((r) => {
      const elapsed = formatDuration(r.elapsedMs);
      const cm = r.currentMissionName || "—";
      const done = r.finished ? "Yes" : "No";
      return `<tr>
        <td>${r.rank}</td>
        <td>${escapeHtml(r.teamName)}</td>
        <td>${r.completedMissions}</td>
        <td class="mono">${escapeHtml(elapsed)}</td>
        <td>${escapeHtml(cm)}</td>
        <td>${done}</td>
      </tr>`;
    })
    .join("");
}

function coachEntered() {
  return sessionStorage.getItem(STORAGE.coachEntered) === "1";
}

function setCoachEntered() {
  sessionStorage.setItem(STORAGE.coachEntered, "1");
}

function getStoredTeamId() {
  return localStorage.getItem(STORAGE.teamId);
}

function setStoredTeamId(teamId) {
  localStorage.setItem(STORAGE.teamId, teamId);
}

function clearStoredTeamId() {
  localStorage.removeItem(STORAGE.teamId);
}

function routeKind() {
  const p = location.pathname || "/";
  if (p.startsWith("/join")) return "participant";
  if (p.startsWith("/print")) return "print";
  return "coach";
}

function currentTeamViewPath() {
  const p = location.pathname || "/join";
  if (p === "/join" || p === "/join/") return "join";
  if (p.startsWith("/join/dashboard")) return "dashboard";
  if (p.startsWith("/join/complete")) return "complete";
  return "join";
}

function coachView() {
  const phase = publicState?.phase || "config";
  if (phase === "registration") return "registration";
  if (phase === "live") return "leaderboard";
  if (!coachEntered()) return "welcome";
  return "config";
}

function renderWelcome() {
  const node = viewCard(
    "WELCOME",
    "CJM GLOBAL INTEGRITY HUB",
    `
      <div class="hero-logo-wrap">
        <img class="hero-logo" src="${assetUrl("logo.svg")}" alt="CJM Global Integrity Hub logo" />
      </div>
      <div class="hint">
        <strong>Coach / Trainer</strong>
        <span>Press ENTER to configure today's games.</span>
      </div>
      <div class="btn-row">
        <button class="primary" data-action="enter">ENTER</button>
        <a class="btn" href="/print">Print QR</a>
      </div>
    `
  );
  qs('[data-action="enter"]', node).addEventListener("click", () => {
    setCoachEntered();
    render();
  });
  return node;
}

function renderConfig() {
  const all = publicState?.allGames || [];
  const max = publicState?.maxGames || 8;
  const selected = new Set(publicState?.selectedGames || []);

  const node = viewCard(
    "Configure Today's Games",
    "Please choose the games that will be played today.",
    `
      <div class="grid">
        <div class="chips" data-role="game-chips"></div>
        <div class="hint" data-role="select-hint"></div>
        <div class="btn-row">
          <button class="primary" data-action="continue" disabled>CONTINUE</button>
        </div>
        <div class="footer-note">
          Rules: Coach must select exactly ${max} games. Registration opens immediately after Continue.
        </div>
      </div>
    `
  );

  const chips = qs('[data-role="game-chips"]', node);
  const hint = qs('[data-role="select-hint"]', node);
  const btn = qs('[data-action="continue"]', node);

  function updateUi() {
    const count = selected.size;
    let msg = `Selected: ${count} / ${max}`;
    let kind = count === max ? "good" : "";
    if (count < max) msg += ` (select ${max - count} more)`;
    if (count > max) {
      msg = `Maximum ${max} Games Only`;
      kind = "bad";
    }
    hint.className = `hint ${kind === "bad" ? "bad" : ""}`;
    hint.textContent = msg;
    btn.disabled = count !== max;
  }

  chips.innerHTML = all
    .map((g) => {
      const id = g.id;
      const checked = selected.has(id) ? "checked" : "";
      return `
        <div class="chip">
          <input type="checkbox" id="g_${escapeHtml(id)}" data-game="${escapeHtml(id)}" ${checked} />
          <label for="g_${escapeHtml(id)}">${escapeHtml(g.name)}</label>
        </div>
      `;
    })
    .join("");

  chips.addEventListener("change", (e) => {
    const input = e.target;
    if (!(input instanceof HTMLInputElement)) return;
    const id = input.getAttribute("data-game");
    if (!id) return;
    if (input.checked) selected.add(id);
    else selected.delete(id);
    updateUi();
  });

  updateUi();

  btn.addEventListener("click", async () => {
    const selectedGames = Array.from(selected);
    btn.disabled = true;
    try {
      await fetchJson(API.selectGames, {
        method: "POST",
        body: JSON.stringify({ selectedGames }),
      });
      toast("Games configured. Registration is open.");
    } catch (e) {
      toast(e.message || "Failed to configure games", "bad");
    } finally {
      btn.disabled = selected.size !== max;
    }
  });

  return node;
}

function renderRegistration() {
  const teams = publicState?.registeredTeams || [];
  const url = joinUrl();

  const node = viewCard(
    "Registration",
    "Participants scan this QR and enter Team Name.",
    `
      <div class="split">
        <div class="card" style="box-shadow:none;background:transparent;border:none;">
          <div class="card-inner" style="padding:0;">
            <div class="qr-box"><div class="qr-render" data-role="reg-qr" aria-label="Registration QR code"></div></div>
            <div class="hint" style="margin-top:12px;">
              <strong>Registration Link</strong>
              <span class="mono" style="word-break:break-all;">${escapeHtml(url)}</span>
            </div>
            <div class="btn-row" style="margin-top:12px;">
              <button class="primary" data-action="start">START THE GAME</button>
              <a class="btn" href="/print">Print QR</a>
            </div>
          </div>
        </div>
        <div>
          <h2 class="h2" style="margin:0 0 10px;">Registered Teams</h2>
          <div class="list" data-role="team-list"></div>
          <div class="footer-note">Registration closes immediately when Coach starts the game.</div>
        </div>
      </div>
    `
  );

  const qrHost = qs('[data-role="reg-qr"]', node);
  renderQrNode(qrHost, url, 280, "Registration QR failed to render.");

  const list = qs('[data-role="team-list"]', node);
  if (teams.length === 0) {
    list.appendChild(el(`<div class="hint">No teams registered yet.</div>`));
  } else {
    teams.forEach((t, idx) => {
      list.appendChild(
        el(`<div class="row"><strong>${idx + 1}. ${escapeHtml(t.teamName)}</strong><span class="mono">${escapeHtml(t.teamId.slice(0, 6))}</span></div>`)
      );
    });
  }

  qs('[data-action="start"]', node).addEventListener("click", async () => {
    const button = qs('[data-action="start"]', node);
    button.disabled = true;
    try {
      await fetchJson(API.start, { method: "POST", body: JSON.stringify({}) });
      toast("Game started. Live leaderboard activated.");
    } catch (e) {
      toast(e.message || "Failed to start game", "bad");
      button.disabled = false;
    }
  });

  return node;
}

function renderLeaderboard() {
  const node = viewCard(
    "LIVE LEADERBOARD",
    "Updates automatically. No refresh required.",
    `
      <div class="btn-row no-print" style="justify-content:space-between;">
        <div class="pill"><span class="dot"></span><span>Live</span></div>
        <button class="danger" data-action="shutdown">SHUT THE GAME</button>
      </div>
      <div style="height:12px;"></div>
      <table class="table" data-role="leaderboard-table">
        <thead>
          <tr>
            <th>Rank</th>
            <th>Team Name</th>
            <th>Completed</th>
            <th>Elapsed</th>
            <th>Current Mission</th>
            <th>Finished</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    `
  );
  renderLeaderboardIfVisible();
  qs('[data-action="shutdown"]', node).addEventListener("click", async () => {
    const button = qs('[data-action="shutdown"]', node);
    button.disabled = true;
    try {
      await fetchJson(API.shutdown, { method: "POST", body: JSON.stringify({}) });
      sessionStorage.removeItem(STORAGE.coachEntered);
      toast("Game ended. System reset.");
    } catch (e) {
      toast(e.message || "Failed to shut down", "bad");
      button.disabled = false;
    }
  });
  return node;
}

function renderParticipantJoin() {
  const node = viewCard(
    "Welcome",
    "Enter Team Name",
    `
      <div class="field">
        <div class="label">Team Name</div>
        <input type="text" inputmode="text" autocomplete="off" maxlength="40" placeholder="e.g. Alpha" data-role="team-name" />
      </div>
      <div class="btn-row" style="margin-top:12px;">
        <button class="primary" data-action="join">JOIN</button>
      </div>
      <div class="footer-note">No login. No email. No password.</div>
    `
  );

  const input = qs('[data-role="team-name"]', node);
  const btn = qs('[data-action="join"]', node);
  btn.addEventListener("click", async () => {
    const teamName = String(input.value || "").trim();
    if (!teamName) {
      toast("Please enter your team name.", "bad");
      return;
    }
    btn.disabled = true;
    try {
      const res = await fetchJson(API.register, { method: "POST", body: JSON.stringify({ teamName }) });
      setStoredTeamId(res.teamId);
      toast("Registered. Waiting for Coach...");
      setPath("/join/dashboard");
    } catch (e) {
      toast(e.message || "Registration failed", "bad");
      btn.disabled = false;
    }
  });

  setTimeout(() => input?.focus(), 50);
  return node;
}

function renderParticipantWaiting(team) {
  const node = viewCard(
    "Waiting For Coach...",
    "Please wait until the Coach starts the game.",
    `
      <div class="hint">
        <strong>Team</strong>
        <span>${escapeHtml(team?.teamName || "—")}</span>
      </div>
      <div class="footer-note">This screen will update automatically when the game starts.</div>
    `
  );
  return node;
}

function renderParticipantComplete(team) {
  const code = team?.collectedCode || "";
  const ms = team?.elapsedMs;
  const node = viewCard(
    "Congratulations",
    "Mission Completed",
    `
      <div class="grid">
        <div class="hint good"><strong>Team</strong><span>${escapeHtml(team?.teamName || "—")}</span></div>
        <div class="hint"><strong>Completion Time</strong><span class="mono">${escapeHtml(formatDuration(ms))}</span></div>
        <div class="hint"><strong>Collected Code</strong><span class="mono">${escapeHtml(code)}</span></div>
        <div class="btn-row">
          <a class="btn" href="/join" data-action="restart">Back</a>
        </div>
      </div>
    `
  );
  qs('[data-action="restart"]', node).addEventListener("click", () => {
    clearStoredTeamId();
  });
  return node;
}

function progressUi(team) {
  const total = Array.isArray(team?.route) ? team.route.length : 0;
  const done = team?.completedMissions ?? 0;
  const pct = total > 0 ? clamp((done / total) * 100, 0, 100) : 0;
  const host = el(`
    <div class="grid">
      <div class="row">
        <div>
          <strong>${escapeHtml(team?.teamName || "—")}</strong>
          <div style="height:6px;"></div>
          <span>Current Mission: ${escapeHtml(team?.currentMissionName || "—")}</span>
        </div>
        <div style="text-align:right;">
          <div class="mono">${done}/${total}</div>
          <div style="height:6px;"></div>
          <span class="mono">${escapeHtml(team?.collectedCode || "")}</span>
        </div>
      </div>
      <div class="progress"><div style="width:${pct}%;"></div></div>
    </div>
  `);
  return host;
}

function createScanner(onText) {
  const canDetect = "BarcodeDetector" in window && !!navigator.mediaDevices?.getUserMedia;
  const host = el(`
    <div class="grid">
      <div class="hint">
        <strong>SCAN QR</strong>
        <span>${canDetect ? "Allow camera access and point at the QR." : "Tap OPEN CAMERA to capture a QR image with your phone camera."}</span>
      </div>
      <div class="scanner" data-role="scanner"></div>
      <input type="file" accept="image/*" capture="environment" data-role="capture" hidden />
      <div class="btn-row">
        <button class="primary" data-action="open-camera">${canDetect ? "OPEN CAMERA" : "CAPTURE QR"}</button>
        <button data-action="stop">STOP</button>
      </div>
      <div class="footer-note">No manual QR typing. On iPhone Safari and older phones, camera capture is decoded on the server automatically.</div>
    </div>
  `);

  let stream = null;
  let raf = 0;
  let lastHitAt = 0;
  const scanBox = qs('[data-role="scanner"]', host);
  const captureInput = qs('[data-role="capture"]', host);
  const openBtn = qs('[data-action="open-camera"]', host);
  const stopBtn = qs('[data-action="stop"]', host);
  let scanBusy = false;

  async function decodeCapturedFile(file) {
    if (!file || scanBusy) return;
    if (scanBusy) return;
    scanBusy = true;
    openBtn.disabled = true;
    const form = new FormData();
    form.append("image", file, file.name || "capture.jpg");
    try {
      const res = await fetch(API.decodeQr, { method: "POST", body: form });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        const msg = data?.error?.message || "Unable to decode this QR image.";
        throw new Error(msg);
      }
      await onText(data.scannedText);
    } catch (e) {
      toast(e.message || "Unable to decode this QR image.", "bad");
    } finally {
      scanBusy = false;
      openBtn.disabled = false;
      captureInput.value = "";
    }
  }

  async function startCamera() {
    if (!canDetect) return;
    const video = document.createElement("video");
    video.setAttribute("playsinline", "true");
    scanBox.innerHTML = "";
    scanBox.appendChild(video);
    stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
    video.srcObject = stream;
    await video.play();
    const detector = new BarcodeDetector({ formats: ["qr_code"] });
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d", { willReadFrequently: true });

    const tick = async () => {
      raf = requestAnimationFrame(tick);
      if (!video.videoWidth || !video.videoHeight) return;
      const now = Date.now();
      if (now - lastHitAt < 800) return;
      const w = video.videoWidth;
      const h = video.videoHeight;
      canvas.width = w;
      canvas.height = h;
      ctx.drawImage(video, 0, 0, w, h);
      let codes = [];
      try {
        codes = await detector.detect(canvas);
      } catch {
        codes = [];
      }
      const raw = codes?.[0]?.rawValue;
      if (raw && !scanBusy) {
        scanBusy = true;
        lastHitAt = now;
        onText(raw);
        setTimeout(() => {
          scanBusy = false;
        }, 900);
      }
    };
    tick();
  }

  function stop() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      stream = null;
    }
    scanBox.innerHTML = `<div class="hint">Scanner stopped.</div>`;
    openBtn.disabled = false;
  }

  captureInput.addEventListener("change", () => {
    const file = captureInput.files?.[0];
    decodeCapturedFile(file).catch(() => {});
  });
  openBtn.addEventListener("click", () => {
    if (canDetect) {
      startCamera().catch(() => {
        captureInput.click();
      });
      return;
    }
    captureInput.click();
  });
  stopBtn.addEventListener("click", stop);
  setTimeout(() => {
    if (canDetect) startCamera().catch(() => {});
  }, 10);
  return { host, stop };
}

async function renderParticipantDashboard(team) {
  const node = viewCard(
    "Participant Dashboard",
    "Complete your current mission, then scan the station QR.",
    `
      <div data-role="team-summary"></div>
      <div class="btn-row">
        <button class="primary" data-action="scan">SCAN QR</button>
        <button data-action="refresh">REFRESH</button>
        <button class="danger" data-action="leave">LEAVE</button>
      </div>
      <div data-role="scan-area" style="margin-top:12px;"></div>
      <div data-role="result" style="margin-top:12px;"></div>
    `
  );

  const summary = qs('[data-role="team-summary"]', node);
  const scanArea = qs('[data-role="scan-area"]', node);
  const result = qs('[data-role="result"]', node);
  summary.appendChild(progressUi(team));

  let scanner = null;

  async function loadTeam() {
    const id = getStoredTeamId();
    if (!id) return null;
    try {
      return await fetchJson(API.team(id));
    } catch (e) {
      if (e?.code === "event_reset") {
        clearStoredTeamId();
        queueNotice("The event is no longer available. Please wait for the coach to start a new session.");
      }
      return null;
    }
  }

  async function handleScanText(text) {
    const id = getStoredTeamId();
    if (!id) return;
    try {
      const res = await fetchJson(API.scan, { method: "POST", body: JSON.stringify({ teamId: id, scannedText: text }) });
      result.innerHTML = `<div class="hint good"><strong>Mission Verified</strong><span>Letter Collected: <span class="mono">${escapeHtml(res.letter)}</span></span></div>`;
      toast("Mission verified. Next mission unlocked.");
    } catch (e) {
      if (e?.code === "wrong_mission") {
        const expected = e.data?.error?.expectedName || "your current mission";
        result.innerHTML = `<div class="hint bad"><strong>Wrong Mission</strong><span>Please complete your current mission first: ${escapeHtml(expected)}</span></div>`;
      } else if (e?.code === "duplicate_scan") {
        result.innerHTML = `<div class="hint bad"><strong>Duplicate Scan</strong><span>This station QR was already used by your team.</span></div>`;
      } else if (e?.code === "event_reset") {
        clearStoredTeamId();
        queueNotice("The event ended or the server restarted. Please join again when the coach opens a new session.");
        setPath("/join");
        return;
      } else {
        result.innerHTML = `<div class="hint bad"><strong>Error</strong><span>${escapeHtml(e.message || "Scan failed")}</span></div>`;
      }
      toast(e.message || "Scan failed", "bad");
    } finally {
      const updated = await loadTeam();
      if (updated) {
        summary.innerHTML = "";
        summary.appendChild(progressUi(updated));
        if (updated.finishedAt) {
          setPath("/join/complete");
        }
      }
    }
  }

  qs('[data-action="scan"]', node).addEventListener("click", () => {
    scanArea.innerHTML = "";
    result.innerHTML = "";
    if (scanner) {
      scanner.stop();
      scanner = null;
    }
    scanner = createScanner(handleScanText);
    activeScannerStop = () => {
      if (scanner) {
        scanner.stop();
        scanner = null;
      }
    };
    scanArea.appendChild(scanner.host);
  });

  qs('[data-action="refresh"]', node).addEventListener("click", async () => {
    const updated = await loadTeam();
    if (updated) {
      summary.innerHTML = "";
      summary.appendChild(progressUi(updated));
      toast("Updated.");
    }
  });

  qs('[data-action="leave"]', node).addEventListener("click", () => {
    clearStoredTeamId();
    setPath("/join");
  });

  return node;
}

async function renderPrint() {
  const all = publicState?.allGames || [];
  const node = viewCard(
    "Printable QR Pack",
    "Registration QR + static station QR (A4 friendly).",
    `
      <div class="btn-row no-print">
        <a class="btn" href="/coach">Back to Coach</a>
        <button class="primary" data-action="print" disabled>PREPARING QR...</button>
      </div>
      <div style="height:14px;"></div>
      <div class="grid-2 grid" data-role="print-grid"></div>
      <div class="footer-note">Station QRs never change. Print once and reuse for future events.</div>
    `
  );
  const printBtn = qs('[data-action="print"]', node);
  printBtn.addEventListener("click", () => {
    if (!printBtn.disabled) window.print();
  });
  const grid = qs('[data-role="print-grid"]', node);
  const renderResults = [];

  const reg = el(`
    <div class="card">
      <div class="card-inner">
        <div class="card-header">
          <div>
            <h2 class="h2">Registration QR</h2>
            <p class="sub">Participants scan this to join.</p>
          </div>
        </div>
        <div class="qr-box"><div class="qr-render qr-print" data-role="qr" aria-label="Registration QR code for participants"></div></div>
        <div class="hint" style="margin-top:12px;"><strong>Link</strong><span class="mono" style="word-break:break-all;">${escapeHtml(joinUrl())}</span></div>
      </div>
    </div>
  `);
  grid.appendChild(reg);
  const regQr = qs('[data-role="qr"]', reg);
  renderResults.push(renderQrNode(regQr, joinUrl(), 900, "Registration QR failed to render."));

  all.forEach((g) => {
    const payload = gamePayload(g.id);
    const card = el(`
      <div class="card">
        <div class="card-inner">
          <div class="card-header">
            <div>
              <h2 class="h2">${escapeHtml(g.name)}</h2>
              <p class="sub">Letter: <span class="mono">${escapeHtml(g.letter || "")}</span></p>
            </div>
          </div>
          <div class="qr-box"><div class="qr-render qr-print" data-role="qr" aria-label="${escapeHtml(g.name)} station QR code"></div></div>
          <div class="hint" style="margin-top:12px;"><strong>Payload</strong><span class="mono" style="word-break:break-all;">${escapeHtml(payload)}</span></div>
        </div>
      </div>
    `);
    grid.appendChild(card);
    const c = qs('[data-role="qr"]', card);
    renderResults.push(renderQrNode(c, payload, 900, `${g.name} QR failed to render.`));
  });

  const allReady = renderResults.every(Boolean);
  printBtn.disabled = !allReady;
  printBtn.textContent = allReady ? "PRINT" : "QR LOAD FAILED";
  if (!allReady) {
    toast("Some printable QR codes failed to load. Printing is disabled until they are fixed.", "bad");
  }

  return node;
}

async function resolveParticipantTeam() {
  const id = getStoredTeamId();
  if (!id) return null;
  try {
    return await fetchJson(API.team(id));
  } catch (e) {
    if (e?.code === "event_reset") {
      queueNotice("The event was reset or the server restarted. Please join again when available.");
    }
    clearStoredTeamId();
    return null;
  }
}

async function render() {
  const sequence = ++renderSequence;
  ensureRealtime();
  await ensurePublicState().catch(() => {});
  if (sequence !== renderSequence) return;

  const kind = routeKind();
  let content = null;

  if (kind === "print") {
    content = await renderPrint();
  } else if (kind === "participant") {
    const team = await resolveParticipantTeam();
    const path = currentTeamViewPath();
    const phase = publicState?.phase;
    if (!team && path !== "join") {
      setPath("/join");
      return;
    }
    if (!team) {
      content = renderParticipantJoin();
    } else if (team.finishedAt) {
      content = renderParticipantComplete(team);
    } else if (phase !== "live") {
      content = renderParticipantWaiting(team);
    } else {
      content = await renderParticipantDashboard(team);
    }
  } else {
    const cv = coachView();
    if (cv === "welcome") content = renderWelcome();
    else if (cv === "config") content = renderConfig();
    else if (cv === "registration") content = renderRegistration();
    else content = renderLeaderboard();
  }

  const app = qs("#app");
  cleanupActiveScanner();
  app.innerHTML = "";
  app.appendChild(renderShell(content));
  flushNotice();
}

render();
