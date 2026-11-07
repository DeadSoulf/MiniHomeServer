/* MiniHomeServer dashboard v0.1.0 — no demo data, no device commands. */
"use strict";

const $ = (id) => document.getElementById(id);
const kinds = {
  house: { icon: "⌂", label: "Дом / здание" },
  boiler: { icon: "♨", label: "Котельная" },
  camera: { icon: "◉", label: "Камера" },
  light: { icon: "☀", label: "Освещение" },
  gate: { icon: "⇥", label: "Ворота" },
  sensor: { icon: "◈", label: "Датчик" },
  switch: { icon: "⊙", label: "Реле" },
  other: { icon: "▣", label: "Объект" },
};
const actions = {
  "auth.login": "Вход в панель",
  "map.updated": "Карта обновлена",
  "device.added": "Добавлен объект",
  "device.updated": "Изменён объект",
  "device.deleted": "Удалён объект",
  "update.queued": "Запрошено обновление",
  "update.success": "Установлено обновление",
  "update.failed": "Ошибка обновления",
  "update.rollback": "Откат обновления",
};
let csrf = null;
let state = { devices: [], events: [], map_url: null, integrations: [], version: "—" };
let editing = false;
let placing = false;
let dragging = null;
let currentView = "territory";
let lastMapUrl = null;
let toastTimer = null;
let busy = false;
let updateInfo = null;
let updateLatest = null;
let updateAvailable = false;
let updateChecking = false;
let updateInstalling = false;
let updateRequestedHere = false;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(text, className, onClick) {
  const node = el("button", className, text);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}
function notify(message, isError = false) {
  const box = $("toast");
  box.textContent = message;
  box.classList.toggle("error", isError);
  box.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.remove("visible"), 4500);
}
async function api(path, method = "GET", data = undefined) {
  const headers = {};
  if (data !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && path !== "/api/login") headers["X-CSRF-Token"] = csrf || "";
  const resp = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: data === undefined ? undefined : JSON.stringify(data),
    cache: "no-store",
  });
  let payload = {};
  try { payload = await resp.json(); } catch { /* e.g. a proxy error */ }
  if (!resp.ok) {
    if (resp.status === 401 && path !== "/api/login") showLogin();
    throw new Error(payload.error || "Ошибка подключения к серверу");
  }
  return payload;
}

function showLogin() {
  csrf = null;
  $("app-shell").hidden = true;
  $("auth-screen").hidden = false;
  $("login-password").focus();
}
async function beginSession() {
  const s = await api("/api/session");
  if (!s.authenticated) return showLogin();
  csrf = s.csrf;
  $("auth-screen").hidden = true;
  $("app-shell").hidden = false;
  await refresh();
}
async function refresh() {
  if (!csrf) return;
  try {
    state = await api("/api/state");
    render();
  } catch (error) {
    if (csrf) notify(error.message, true);
  }
}

function navigate(view) {
  currentView = view;
  document.querySelectorAll(".view").forEach((node) => {
    node.classList.toggle("active", node.id === "view-" + view);
  });
  document.querySelectorAll("[data-view]").forEach((node) => {
    node.classList.toggle("active", node.dataset.view === view);
  });
  const titles = { territory: "Территория", devices: "Оборудование", events: "События", settings: "Настройки" };
  $("breadcrumb-current").textContent = titles[view] || view;
  if (view !== "territory") setPlacing(false);
  if (view === "settings" && csrf) loadUpdateStatus();
}

function render() {
  const devices = state.devices;
  const zones = new Set(devices.map((d) => d.zone.trim()).filter(Boolean));
  $("stat-total").textContent = String(devices.length);
  $("stat-zones").textContent = String(zones.size);
  $("stat-unknown").textContent = String(devices.filter((d) => d.state !== "online").length);
  $("stat-integrations").textContent = String(state.integrations.length);
  $("nav-device-count").textContent = String(devices.length);
  $("device-list-total").textContent = devices.length + " объектов";
  $("sidebar-version").textContent = "Версия " + state.version;
  $("settings-version").textContent = state.version;
  renderMap();
  renderDevices();
  renderEvents();
  renderSummary();
}

function statusText(d) {
  if (d.state === "online") return "На связи";
  if (d.state === "offline") return "Офлайн";
  return "Нет данных";
}
function statusClass(d) {
  return d.state === "online" ? "good" : d.state === "offline" ? "bad" : "unknown";
}

function renderMap() {
  const stage = $("map-stage");
  const image = $("map-image");
  const empty = $("map-empty");
  if (state.map_url !== lastMapUrl) {
    lastMapUrl = state.map_url;
    if (state.map_url) {
      image.hidden = false;
      empty.hidden = true;
      image.onload = () => {
        if (image.naturalWidth && image.naturalHeight)
          stage.style.aspectRatio = image.naturalWidth + " / " + image.naturalHeight;
      };
      image.src = state.map_url;
    } else {
      image.hidden = true;
      image.removeAttribute("src");
      empty.hidden = false;
      stage.style.aspectRatio = "16 / 10";
    }
  }
  stage.classList.toggle("editing", editing);
  stage.classList.toggle("placing", placing);
  $("edit-map-btn").classList.toggle("selected", editing);
  $("edit-map-btn").setAttribute("aria-pressed", String(editing));
  $("map-mode-label").textContent = placing
    ? "Нажмите на точку карты, чтобы разместить объект"
    : editing
      ? "Перетаскивайте значки по карте — расположение сохранится"
      : "Нажмите «Редактировать», чтобы перемещать объекты";
  if (dragging) return;
  const layer = $("map-markers");
  layer.replaceChildren();
  for (const d of state.devices) {
    const node = el("button", "map-marker");
    node.type = "button";
    node.style.left = (d.x * 100) + "%";
    node.style.top = (d.y * 100) + "%";
    node.dataset.id = d.id;
    node.title = d.name + " · " + statusText(d);
    node.setAttribute("aria-label", node.title);
    node.classList.add("marker-" + statusClass(d));
    node.append(el("span", "marker-symbol", (kinds[d.kind] || kinds.other).icon));
    node.append(el("span", "marker-name", d.name));
    node.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (node.dataset.justDragged === "1") return;
      openObject(d);
    });
    node.addEventListener("pointerdown", (ev) => startDrag(ev, d, node));
    layer.append(node);
  }
}

function setPlacing(value) {
  placing = Boolean(value);
  $("map-stage").classList.toggle("placing", placing);
  $("map-mode-label").textContent = placing
    ? "Нажмите место на карте для нового объекта"
    : editing ? "Перемещайте значки по карте" : "Нажмите «Редактировать», чтобы перемещать объекты";
}
function mapPosition(event) {
  const rect = $("map-stage").getBoundingClientRect();
  return {
    x: Math.round(Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * 100000) / 100000,
    y: Math.round(Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) * 100000) / 100000,
  };
}
function startDrag(event, d, node) {
  if (!editing || event.button !== 0) return;
  event.preventDefault();
  event.stopPropagation();
  node.setPointerCapture(event.pointerId);
  dragging = { id: d.id, node, startX: event.clientX, startY: event.clientY, moved: false };
  node.classList.add("dragging");
  const move = (ev) => {
    if (!dragging || ev.pointerId !== event.pointerId) return;
    const pos = mapPosition(ev);
    node.style.left = (pos.x * 100) + "%";
    node.style.top = (pos.y * 100) + "%";
    if (Math.hypot(ev.clientX - dragging.startX, ev.clientY - dragging.startY) > 5)
      dragging.moved = true;
  };
  const finish = async (ev) => {
    node.removeEventListener("pointermove", move);
    node.removeEventListener("pointerup", finish);
    node.removeEventListener("pointercancel", cancel);
    if (ev.pointerId !== event.pointerId) return;
    const moved = dragging && dragging.moved;
    dragging = null;
    node.classList.remove("dragging");
    if (!moved) return;
    node.dataset.justDragged = "1";
    setTimeout(() => { node.dataset.justDragged = ""; }, 200);
    try {
      const pos = mapPosition(ev);
      await api("/api/devices/" + encodeURIComponent(d.id), "PATCH", pos);
      await refresh();
      notify("Расположение сохранено");
    } catch (error) {
      notify(error.message, true);
      await refresh();
    }
  };
  const cancel = () => {
    dragging = null;
    node.classList.remove("dragging");
    node.removeEventListener("pointermove", move);
    node.removeEventListener("pointerup", finish);
    node.removeEventListener("pointercancel", cancel);
    renderMap();
  };
  node.addEventListener("pointermove", move);
  node.addEventListener("pointerup", finish);
  node.addEventListener("pointercancel", cancel);
}

function openObject(d = null, position = null) {
  $("object-form").reset();
  $("object-error").textContent = "";
  $("object-id").value = d ? d.id : "";
  $("object-name").value = d ? d.name : "";
  $("object-kind").value = d ? d.kind : "house";
  $("object-zone").value = d ? d.zone : "";
  const x = position ? position.x : d ? d.x : 0.5;
  const y = position ? position.y : d ? d.y : 0.5;
  $("object-x").value = x;
  $("object-y").value = y;
  $("object-coords").textContent = "Положение на карте: " + Math.round(x * 100) + "% / " + Math.round(y * 100) + "%";
  $("dialog-title").textContent = d ? "Изменить объект" : "Новый объект";
  setPlacing(false);
  $("object-dialog").showModal();
  $("object-name").focus();
}

function deviceRow(d, compact = false) {
  const node = el("div", compact ? "summary-row" : "device-row");
  const glyph = el("span", "device-glyph", (kinds[d.kind] || kinds.other).icon);
  const content = el("div", "device-info");
  content.append(el("strong", "", d.name));
  content.append(el("small", "", (d.zone || (kinds[d.kind] || kinds.other).label) + " · " + statusText(d)));
  node.append(glyph, content);
  if (!compact) {
    const status = el("span", "device-status " + statusClass(d), statusText(d));
    const actionsEl = el("div", "device-actions");
    actionsEl.append(button("На карте", "btn subtle small", () => {
      navigate("territory");
      setPlacing(false);
      const marker = [...document.querySelectorAll(".map-marker")].find((m) => m.dataset.id === d.id);
      if (marker) {
        marker.classList.add("focused");
        setTimeout(() => marker.classList.remove("focused"), 2000);
      }
    }));
    actionsEl.append(button("Изменить", "btn secondary small", () => openObject(d)));
    actionsEl.append(button("×", "btn subtle small danger-action", async () => {
      if (!confirm("Удалить объект «" + d.name + "» с карты и из реестра?")) return;
      try {
        await api("/api/devices/" + encodeURIComponent(d.id), "DELETE");
        await refresh();
        notify("Объект удалён");
      } catch (err) { notify(err.message, true); }
    }));
    node.append(status, actionsEl);
  } else {
    node.append(el("span", "mini-chevron", "↗"));
    node.setAttribute("role", "button");
    node.tabIndex = 0;
    node.addEventListener("click", () => openObject(d));
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openObject(d); }
    });
  }
  return node;
}

function renderDevices() {
  const list = $("devices-list");
  list.replaceChildren();
  if (!state.devices.length) {
    const empty = el("div", "list-empty");
    empty.append(el("div", "empty-icon small-icon", "◫"));
    empty.append(el("strong", "", "Оборудование пока не добавлено"));
    empty.append(el("p", "", "Создайте первый объект и разместите его на карте территории."));
    empty.append(button("＋ Добавить объект", "btn primary", () => openObject()));
    list.append(empty);
    return;
  }
  for (const d of state.devices) list.append(deviceRow(d));
}
function eventRow(event, compact = false) {
  const row = el("div", compact ? "summary-row event-row" : "event-entry");
  row.append(el("span", "event-mark", "•"));
  const body = el("div", "event-text");
  body.append(el("strong", "", actions[event.action] || event.action));
  body.append(el("small", "", event.detail));
  row.append(body);
  row.append(el("time", "event-date", new Date(event.at).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })));
  return row;
}
function renderEvents() {
  const list = $("events-list");
  list.replaceChildren();
  if (!state.events.length) {
    list.append(el("div", "list-empty", "Событий пока нет"));
  } else {
    for (const item of state.events) list.append(eventRow(item));
  }
}
function renderSummary() {
  const dlist = $("summary-devices");
  dlist.replaceChildren();
  for (const d of state.devices.slice(0, 5)) dlist.append(deviceRow(d, true));
  if (!state.devices.length) dlist.append(el("div", "small-empty", "Пока нет объектов. Добавьте их на карту."));
  const events = $("summary-events");
  events.replaceChildren();
  for (const e of state.events.slice(0, 5)) events.append(eventRow(e, true));
  if (!state.events.length) events.append(el("div", "small-empty", "История пока пуста."));
}

async function uploadMap(file) {
  if (!file) return;
  if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
    return notify("Поддерживаются только PNG, JPEG и WebP", true);
  }
  if (file.size > 8 * 1024 * 1024 || file.size <= 0) {
    return notify("Максимальный размер файла — 8 МБ", true);
  }
  if (busy) return;
  busy = true;
  notify("Загружаем карту территории…");
  try {
    const data_url = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("Не удалось прочитать файл"));
      reader.readAsDataURL(file);
    });
    await api("/api/map", "POST", { data_url });
    await refresh();
    notify("Карта загружена");
    navigate("territory");
  } catch (err) {
    notify(err.message, true);
  } finally {
    busy = false;
    $("map-file").value = "";
  }
}


function shortCommit(sha) {
  return typeof sha === "string" && /^[a-f0-9]{40}$/.test(sha)
    ? sha.substring(0, 12) : "Неизвестна";
}
const phaseLabels = {
  idle: "Готово к проверке",
  queued: "В очереди",
  running: "Устанавливается",
  success: "Установлено",
  failed: "Ошибка обновления",
  rolled_back: "Выполнен откат",
};
function showUpdateStatus() {
  if (!updateInfo) return;
  const badge = $("update-badge");
  const stage = updateInfo.state || "idle";
  badge.textContent = updateInfo.enabled ? (phaseLabels[stage] || "Готово") : "Не настроено";
  badge.className = "update-badge" + (stage === "failed" || stage === "rolled_back" ? " update-alert"
    : stage === "success" ? " update-ok" : "");
  $("update-current").textContent = shortCommit(updateInfo.current_sha);
  $("update-latest").textContent = updateLatest ? shortCommit(updateLatest) : "Не проверялась";
  if (!updateInfo.enabled) {
    $("update-message").textContent = "Обновления ещё не активированы на Debian. Один раз выполните scripts/enable-web-updates.sh (инструкция в GitHub).";
  } else {
    $("update-message").textContent = updateInfo.message || "Готово к проверке GitHub.";
  }
  const active = stage === "queued" || stage === "running";
  $("update-check").disabled = active || updateChecking || updateInstalling;
  $("update-check").textContent = updateChecking ? "Проверяем GitHub…" : "Проверить GitHub";
  $("update-install").disabled = !updateInfo.enabled || !updateAvailable || !updateLatest || active || updateInstalling || updateChecking;
  $("update-install").textContent = active || updateInstalling ? "Идёт обновление…" : "Установить обновление";
}
async function loadUpdateStatus() {
  if (!csrf || !$("update-current")) return;
  try {
    const next = await api("/api/update/status");
    const wasRunning = updateInfo && ["queued", "running"].includes(updateInfo.state);
    updateInfo = next;
    if (updateLatest && next.current_sha === updateLatest) updateAvailable = false;
    showUpdateStatus();
    if (updateRequestedHere && wasRunning && next.state === "success") {
      updateRequestedHere = false;
      notify("Обновление завершено. Загружаем новую версию панели.");
      setTimeout(() => window.location.reload(), 1100);
    }
    if (updateRequestedHere && (next.state === "failed" || next.state === "rolled_back")) {
      updateRequestedHere = false;
      notify(next.message || "Обновление не завершилось", true);
    }
  } catch (error) {
    if (updateInfo && ["queued", "running"].includes(updateInfo.state)) {
      $("update-message").textContent = "Панель перезапускается — ждём восстановления соединения…";
    } else if (csrf) {
      $("update-message").textContent = error.message;
    }
  }
}
async function checkForUpdate() {
  if (updateChecking || !csrf) return;
  updateChecking = true;
  showUpdateStatus();
  try {
    const data = await api("/api/update/check");
    updateLatest = data.latest_sha;
    updateAvailable = !!data.update_available;
    if (updateInfo) {
      updateInfo.message = updateAvailable
        ? "Найдена новая версия GitHub. Можно установить вручную."
        : "На сервере установлена актуальная версия GitHub.";
    }
    showUpdateStatus();
  } catch (error) {
    notify(error.message, true);
    $("update-message").textContent = error.message;
  } finally {
    updateChecking = false;
    showUpdateStatus();
  }
}
async function installUpdate() {
  if (!updateInfo || !updateInfo.enabled || !updateAvailable || !updateLatest || updateInstalling) return;
  const nextVersion = shortCommit(updateLatest);
  if (!confirm("Установить обновление " + nextVersion + " из GitHub?\n\nMiniHomeServer ненадолго перезапустится. Карта и настройки сохранятся. При ошибке будет предпринята попытка отката.")) return;
  updateInstalling = true;
  showUpdateStatus();
  try {
    await api("/api/update/install", "POST", { sha: updateLatest });
    updateRequestedHere = true;
    updateInfo.state = "queued";
    updateInfo.message = "Запрос отправлен системной службе обновлений.";
    showUpdateStatus();
    notify("Обновление запущено");
    setTimeout(loadUpdateStatus, 900);
  } catch (error) {
    notify(error.message, true);
    await loadUpdateStatus();
  } finally {
    updateInstalling = false;
    showUpdateStatus();
  }
}

function installHandlers() {
  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const error = $("login-error");
    error.textContent = "";
    try {
      const login = await api("/api/login", "POST", { password: $("login-password").value });
      csrf = login.csrf;
      $("login-password").value = "";
      $("auth-screen").hidden = true;
      $("app-shell").hidden = false;
      await refresh();
    } catch (err) { error.textContent = err.message; }
  });
  $("update-check").addEventListener("click", checkForUpdate);
  $("update-install").addEventListener("click", installUpdate);
  $("logout-btn").addEventListener("click", async () => {
    try { await api("/api/logout", "POST", {}); }
    catch { /* clear local session anyway */ }
    showLogin();
  });
  document.querySelectorAll("[data-view]").forEach((node) => {
    node.addEventListener("click", () => navigate(node.dataset.view));
  });
  document.querySelectorAll("[data-go]").forEach((node) => {
    node.addEventListener("click", () => navigate(node.dataset.go));
  });
  $("edit-map-btn").addEventListener("click", () => {
    editing = !editing;
    setPlacing(false);
    renderMap();
  });
  $("add-object-btn").addEventListener("click", () => {
    navigate("territory");
    if (state.map_url) {
      setPlacing(true);
      renderMap();
      notify("Нажмите на место на карте для нового объекта");
    } else openObject();
  });
  $("devices-add-btn").addEventListener("click", () => openObject());
  $("map-stage").addEventListener("click", (e) => {
    if (!placing || e.target.closest(".map-marker")) return;
    openObject(null, mapPosition(e));
  });
  $("map-file").addEventListener("change", (e) => uploadMap(e.target.files[0]));
  $("dialog-close").addEventListener("click", () => $("object-dialog").close());
  $("dialog-cancel").addEventListener("click", () => $("object-dialog").close());
  $("object-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = $("object-id").value;
    const data = {
      name: $("object-name").value.trim(),
      kind: $("object-kind").value,
      zone: $("object-zone").value.trim(),
      x: Number($("object-x").value),
      y: Number($("object-y").value),
    };
    const save = $("dialog-save");
    save.disabled = true;
    $("object-error").textContent = "";
    try {
      if (id) await api("/api/devices/" + encodeURIComponent(id), "PATCH", data);
      else await api("/api/devices", "POST", data);
      $("object-dialog").close();
      await refresh();
      notify(id ? "Объект обновлён" : "Объект добавлен");
    } catch (error) {
      $("object-error").textContent = error.message;
    } finally { save.disabled = false; }
  });
  const updateClock = () => {
    $("clock").textContent = new Date().toLocaleString("ru-RU", {
      day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit"
    });
  };
  updateClock();
  setInterval(updateClock, 15000);
  setInterval(() => {
    if (csrf && currentView === "settings") loadUpdateStatus();
  }, 3000);
  setInterval(() => {
    if (csrf && !dragging && !$("object-dialog").open && !busy) refresh();
  }, 20000);
}
installHandlers();
beginSession().catch((e) => {
  showLogin();
  notify("Нет подключения к серверу: " + e.message, true);
});
