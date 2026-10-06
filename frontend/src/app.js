"use strict";

const STATE_LABEL = {
  empty: "空槽位",
  written: "候选已写入",
  verified: "候选已校验",
  confirmed: "已确认",
  superseded: "已被取代",
  corrupt: "摘要损坏",
};

const VERDICT_LABEL = {
  boot: "可引导",
  no_boot: "停机（无合格清单）",
  ambiguous: "裁决中止（清单不唯一）",
};

const STORAGE_KEY = "payload.console.device";
let currentId = localStorage.getItem(STORAGE_KEY) || null;
let currentView = null;

const $ = (id) => document.getElementById(id);
const short = (d) => (d ? d.slice(0, 12) + "…" : "—");

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = data.error || {};
    const err = new Error(e.message || res.statusText);
    err.code = e.code || `HTTP_${res.status}`;
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(text, ok = true) {
  const el = $("toast");
  el.textContent = text;
  el.className = ok ? "show ok" : "show err";
  setTimeout(() => { el.className = ""; }, 4200);
}

async function refreshDevices() {
  const { devices } = await api("/api/devices");
  const ul = $("device-list");
  ul.innerHTML = "";
  if (!devices.length) {
    ul.innerHTML = '<li class="muted">暂无设备</li>';
    return;
  }
  for (const d of devices) {
    const li = document.createElement("li");
    li.className = d.id === currentId ? "selected" : "";
    li.innerHTML =
      `<span class="dev-name"></span>` +
      `<span class="dev-meta">v${d.active_version} · 槽${d.active_slot} · 代次${d.generation}</span>`;
    li.querySelector(".dev-name").textContent = d.name;
    li.onclick = () => selectDevice(d.id);
    ul.appendChild(li);
  }
}

async function selectDevice(id) {
  currentId = id;
  localStorage.setItem(STORAGE_KEY, id);
  await reopenView();
  await refreshDevices();
}

async function reopenView() {
  if (!currentId) return;
  try {
    currentView = await api(`/api/devices/${currentId}`);
    renderDevice(currentView);
    await refreshEvents();
  } catch (e) {
    if (e.status === 404) {
      currentId = null;
      currentView = null;
      localStorage.removeItem(STORAGE_KEY);
      $("device-detail").hidden = true;
      $("empty-hint").hidden = false;
    } else {
      toast(`加载失败：${e.message}`, false);
    }
  }
}

function slotCard(s, activeSlot) {
  const div = document.createElement("div");
  div.className = `slot state-${s.state}` + (s.slot === activeSlot ? " active" : "");
  const diag = s.diagnostics
    ? `<div class="diag">诊断证据：${s.diagnostics.detail || s.diagnostics.reason}<br>` +
      `期望 <code>${short(s.diagnostics.expected_digest)}</code> · ` +
      `实际 <code>${short(s.diagnostics.actual_digest)}</code></div>`
    : "";
  div.innerHTML = `
    <div class="slot-head">
      <span class="slot-name">槽位 ${s.slot}</span>
      <span class="badge">${STATE_LABEL[s.state] || s.state}</span>
      ${s.slot === activeSlot ? '<span class="badge active-badge">活动</span>' : ""}
    </div>
    <dl>
      <dt>版本</dt><dd>${s.version ?? "—"}</dd>
      <dt>期望摘要</dt><dd><code title="${s.expected_digest || ""}">${short(s.expected_digest)}</code></dd>
      <dt>实际摘要</dt><dd><code title="${s.actual_digest || ""}">${short(s.actual_digest)}</code></dd>
      <dt>摘要一致</dt><dd>${s.expected_digest ? (s.digest_ok ? "✓" : "✗ 不符") : "—"}</dd>
      <dt>确认代次</dt><dd>${s.confirmed_generation ?? "—"}</dd>
      <dt>镜像大小</dt><dd>${s.image_size} B</dd>
    </dl>${diag}`;
  return div;
}

function renderDevice(v) {
  $("empty-hint").hidden = true;
  $("device-detail").hidden = false;
  $("ov-name").textContent = v.name;
  $("ov-id").textContent = v.id;
  $("ov-gen").textContent = v.generation;
  $("ov-active").textContent = v.active_slot;

  const rec = v.recovery;
  const verdictEl = $("ov-verdict");
  verdictEl.textContent = VERDICT_LABEL[rec.verdict] +
    (rec.verdict === "boot" ? `：槽位 ${rec.boot_slot} v${rec.boot_version}` : "");
  verdictEl.className = "verdict " + rec.verdict;

  const wrap = $("slots");
  wrap.innerHTML = "";
  for (const s of v.slots) wrap.appendChild(slotCard(s, v.active_slot));

  const cand = v.slots.find((s) => s.slot !== v.active_slot);
  const stageText = {
    empty: "无候选",
    written: "候选已写入",
    verified: "候选已校验",
    confirmed: "—",
    superseded: "无候选（旧版本已被取代）",
    corrupt: "候选摘要损坏",
  }[cand.state];
  $("btn-power").textContent = `模拟断电（当前阶段：${stageText}）`;
  $("btn-verify").disabled = !["written", "verified", "corrupt"].includes(cand.state);
  $("btn-confirm").disabled = cand.state !== "verified";
  $("cand-gen").value = v.generation;

  $("rec-summary").textContent = rec.summary;
  $("rec-consistent").textContent = rec.consistent
    ? `持久化活动槽位（${rec.persisted_active_slot}）与裁决一致 · 裁决时间 ${rec.decided_at}`
    : `注意：持久化活动槽位 ${rec.persisted_active_slot} 与裁决结果不一致`;
  const tbody = $("rec-checks");
  tbody.innerHTML = "";
  for (const c of rec.checks) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${c.slot}</td><td>${STATE_LABEL[c.state] || c.state}</td>` +
      `<td>${c.version ?? "—"}</td><td>${c.digest_ok ? "✓" : "✗"}</td>` +
      `<td>${c.eligible ? "✓" : "✗"}</td><td></td>`;
    tr.lastElementChild.textContent = c.reason;
    tbody.appendChild(tr);
  }
}

async function refreshEvents() {
  if (!currentId) return;
  const { events } = await api(`/api/devices/${currentId}/events`);
  const ul = $("events");
  ul.innerHTML = "";
  for (const e of events) {
    const li = document.createElement("li");
    li.innerHTML =
      `<span class="ev-kind">${e.kind}</span> <span class="ev-time">${e.created_at}</span>` +
      `<br><code></code>`;
    li.querySelector("code").textContent = JSON.stringify(e.detail);
    ul.appendChild(li);
  }
}

async function run(action, okMsg) {
  try {
    await action();
    await reopenView();
    await refreshDevices();
    if (okMsg) toast(okMsg);
  } catch (e) {
    toast(`${e.code}：${e.message}`, false);
    try { await reopenView(); } catch (_) { /* 忽略刷新失败 */ }
  }
}

function wire() {
  $("btn-create").onclick = () => run(async () => {
    const v = await api("/api/devices", {
      method: "POST",
      body: JSON.stringify({
        name: $("dev-name").value.trim(),
        version: parseInt($("dev-version").value, 10),
        image: $("dev-image").value,
      }),
    });
    currentId = v.id;
    localStorage.setItem(STORAGE_KEY, v.id);
  }, "设备已创建（双槽，槽位 A 已确认）");

  $("btn-write").onclick = () => run(async () => {
    await api(`/api/devices/${currentId}/candidates`, {
      method: "POST",
      body: JSON.stringify({
        version: parseInt($("cand-version").value, 10),
        image: $("cand-image").value,
        expected_generation: currentView.generation,
      }),
    });
  }, "候选已写入并持久化");

  $("btn-verify").onclick = () => run(async () => {
    await api(`/api/devices/${currentId}/verify`, { method: "POST", body: "{}" });
  }, "摘要校验完成");

  $("btn-confirm").onclick = () => run(async () => {
    await api(`/api/devices/${currentId}/confirm`, {
      method: "POST",
      body: JSON.stringify({ expected_generation: currentView.generation }),
    });
  }, "切换已确认，确认代次已推进");

  $("btn-power").onclick = () => run(async () => {
    await api(`/api/devices/${currentId}/power-loss`, {
      method: "POST",
      body: JSON.stringify({ corrupt_candidate: $("corrupt").checked }),
    });
  }, "已模拟断电，恢复裁决已生成");

  $("btn-reopen").onclick = () => run(async () => {}, "已重新打开设备视图");
}

async function pollHealth() {
  const el = $("health");
  try {
    await api("/healthz");
    el.textContent = "服务健康";
    el.className = "health ok";
  } catch (_) {
    el.textContent = "服务不可达";
    el.className = "health err";
  }
}

wire();
pollHealth();
setInterval(pollHealth, 5000);
refreshDevices().catch(() => {});
if (currentId) reopenView();
