const state = {
  view: "articles",
  sources: [],
  keywords: [],
  articleOffset: 0,
  articleLimit: 50,
  articleTotal: 0,
  wasRunning: false,
  toastTimer: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const body = await response.json();
      message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}

function formatFullTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date);
}

function statusLabel(status) {
  const labels = { success: "成功", partial: "部分失败", failed: "失败", running: "运行中", interrupted: "已中断" };
  return `<span class="status-chip status-${escapeHtml(status)}">${labels[status] || escapeHtml(status)}</span>`;
}

function triggerLabel(trigger) {
  return trigger === "scheduled" ? "定时" : "手动";
}

function refreshIcons() {
  if (window.lucide) window.lucide.createIcons();
}

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast show${isError ? " error" : ""}`;
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => { toast.className = "toast"; }, 3200);
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  document.querySelectorAll(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
  if (view === "articles") loadArticles();
  if (view === "runs") loadRuns();
  if (view === "sources") renderSources();
  if (view === "keywords") renderKeywords();
  if (view === "settings") loadSettings();
}

async function loadStatus() {
  try {
    const data = await api("/api/status");
    $("metric-articles").textContent = data.article_count;
    $("metric-sources").textContent = data.active_source_count;
    $("metric-keywords").textContent = data.active_keyword_count;
    $("metric-next-run").textContent = formatFullTime(data.next_scheduled_at);
    $("header-schedule").textContent = `每日 ${data.schedule_time} · ${data.timezone}`;
    $("metric-last-run").innerHTML = data.latest_run
      ? `${statusLabel(data.latest_run.status)} <span class="muted">${formatFullTime(data.latest_run.started_at)}</span>`
      : "尚未采集";
    const indicator = $("run-indicator");
    indicator.className = `run-indicator ${data.running ? "running" : "idle"}`;
    indicator.querySelector("span:last-child").textContent = data.running ? `任务 #${data.running_run_id} 运行中` : "空闲";
    $("collect-now").disabled = data.running;
    $("collect-now").querySelector("span").textContent = data.running ? "正在采集" : "立即采集";
    if (state.wasRunning && !data.running) {
      await Promise.all([loadCatalogs(), loadArticles(), loadRuns()]);
      showToast("采集任务已结束");
    }
    state.wasRunning = data.running;
    refreshIcons();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function startCollection() {
  const button = $("collect-now");
  button.disabled = true;
  try {
    const data = await api("/api/collections", { method: "POST" });
    state.wasRunning = true;
    showToast(`采集任务 #${data.run_id} 已启动`);
    await loadStatus();
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
  }
}

async function loadCatalogs() {
  const [sourceData, keywordData] = await Promise.all([api("/api/sources"), api("/api/keywords")]);
  state.sources = sourceData.items;
  state.keywords = keywordData.items;
  fillFilters();
  renderSources();
  renderKeywords();
}

function fillFilters() {
  const sourceValue = $("article-source-filter").value;
  const keywordValue = $("article-keyword-filter").value;
  $("article-source-filter").innerHTML = `<option value="">全部 RSS 源</option>${state.sources.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("")}`;
  $("article-keyword-filter").innerHTML = `<option value="">全部关键词</option>${state.keywords.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("")}`;
  $("article-source-filter").value = sourceValue;
  $("article-keyword-filter").value = keywordValue;
}

async function loadArticles() {
  const params = new URLSearchParams({ limit: state.articleLimit, offset: state.articleOffset });
  const q = $("article-search").value.trim();
  const sourceId = $("article-source-filter").value;
  const keywordId = $("article-keyword-filter").value;
  if (q) params.set("q", q);
  if (sourceId) params.set("source_id", sourceId);
  if (keywordId) params.set("keyword_id", keywordId);
  try {
    const data = await api(`/api/articles?${params}`);
    state.articleTotal = data.total;
    $("article-total").textContent = `共 ${data.total} 条`;
    $("article-rows").innerHTML = data.items.map((item) => {
      const keywords = (item.keyword_names || "").split(",").filter(Boolean);
      return `<tr>
        <td><a class="article-title" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a><span class="cell-subtitle">${escapeHtml(item.summary || "")}</span></td>
        <td>${escapeHtml(item.publisher || "-")}<span class="cell-subtitle">${escapeHtml(item.feed_name || "-")}</span></td>
        <td><div class="keyword-list">${keywords.map((name) => `<span class="tag">${escapeHtml(name)}</span>`).join("")}</div></td>
        <td>${formatFullTime(item.published_at)}</td>
        <td>${formatFullTime(item.collected_at)}</td>
      </tr>`;
    }).join("");
    $("article-empty").classList.toggle("hidden", data.items.length > 0);
    const page = Math.floor(state.articleOffset / state.articleLimit) + 1;
    const pages = Math.max(1, Math.ceil(data.total / state.articleLimit));
    $("article-page").textContent = `第 ${page} / ${pages} 页`;
    $("article-prev").disabled = state.articleOffset === 0;
    $("article-next").disabled = state.articleOffset + state.articleLimit >= data.total;
  } catch (error) { showToast(error.message, true); }
}

async function loadRuns() {
  try {
    const data = await api("/api/collections?limit=100");
    $("run-rows").innerHTML = data.items.map((run) => `<tr>
      <td>#${run.id}</td><td>${triggerLabel(run.trigger_type)}</td><td>${statusLabel(run.status)}</td>
      <td>${formatFullTime(run.window_start)}<span class="cell-subtitle">至 ${formatFullTime(run.window_end)}</span></td>
      <td>${run.tasks_succeeded} / ${run.tasks_total}<span class="cell-subtitle">失败 ${run.tasks_failed}</span></td>
      <td>${run.items_matched} / ${run.items_inserted}<span class="cell-subtitle">重复 ${run.duplicates}</span></td>
      <td>${formatFullTime(run.started_at)}</td>
      <td><button class="icon-button run-detail" data-id="${run.id}" type="button" title="查看明细"><i data-lucide="list-tree"></i></button></td>
    </tr>`).join("");
    $("run-empty").classList.toggle("hidden", data.items.length > 0);
    refreshIcons();
  } catch (error) { showToast(error.message, true); }
}

async function openRunDetail(id) {
  try {
    const run = await api(`/api/collections/${id}`);
    $("run-dialog-title").textContent = `采集日志 #${run.id}`;
    $("run-summary").innerHTML = [
      ["状态", statusLabel(run.status)], ["触发", triggerLabel(run.trigger_type)],
      ["读取", run.items_seen], ["命中", run.items_matched], ["新增", run.items_inserted],
    ].map(([label, value]) => `<div><span>${label}</span><strong>${value}</strong></div>`).join("");
    $("run-detail-rows").innerHTML = run.details.map((detail) => `<tr>
      <td>${escapeHtml(detail.source_name)}</td><td>${escapeHtml(detail.keyword_name)}</td><td>${statusLabel(detail.status)}</td>
      <td>${detail.items_seen}</td><td>${detail.skipped_outside_window || 0}</td><td>${detail.items_matched}</td><td>${detail.items_inserted}</td>
      <td class="url-cell" title="${escapeHtml(detail.error_message)}">${escapeHtml(detail.error_message || "-")}</td>
    </tr>`).join("");
    $("run-dialog").showModal();
  } catch (error) { showToast(error.message, true); }
}

function toggleMarkup(type, item) {
  return `<label class="toggle" title="${item.active ? "停用" : "启用"}"><input class="${type}-toggle" data-id="${item.id}" type="checkbox" ${item.active ? "checked" : ""} /><span class="toggle-track"></span></label>`;
}

function renderSources() {
  $("source-rows").innerHTML = state.sources.map((item) => `<tr>
    <td><strong>${escapeHtml(item.name)}</strong></td>
    <td><span class="tag">${item.mode === "search" ? "搜索型" : "直连型"}</span></td>
    <td>${escapeHtml(item.language || "-")}</td><td class="url-cell" title="${escapeHtml(item.url_template)}">${escapeHtml(item.url_template)}</td>
    <td>${toggleMarkup("source", item)}</td>
    <td><div class="row-actions"><button class="icon-button source-edit" data-id="${item.id}" title="编辑" type="button"><i data-lucide="pencil"></i></button><button class="icon-button danger-button source-delete" data-id="${item.id}" title="删除" type="button"><i data-lucide="trash-2"></i></button></div></td>
  </tr>`).join("");
  refreshIcons();
}

function renderKeywords() {
  $("keyword-rows").innerHTML = state.keywords.map((item) => `<tr>
    <td><strong>${escapeHtml(item.name)}</strong></td>
    <td class="url-cell" title="${escapeHtml(item.query)}">${escapeHtml(item.query)}</td>
    <td><div class="keyword-list">${item.match_terms.slice(0, 5).map((term) => `<span class="tag">${escapeHtml(term)}</span>`).join("")}${item.match_terms.length > 5 ? `<span class="tag">+${item.match_terms.length - 5}</span>` : ""}</div></td>
    <td>${toggleMarkup("keyword", item)}</td>
    <td><div class="row-actions"><button class="icon-button keyword-edit" data-id="${item.id}" title="编辑" type="button"><i data-lucide="pencil"></i></button><button class="icon-button danger-button keyword-delete" data-id="${item.id}" title="删除" type="button"><i data-lucide="trash-2"></i></button></div></td>
  </tr>`).join("");
  refreshIcons();
}

function openSourceDialog(item = null) {
  $("source-dialog-title").textContent = item ? "编辑 RSS 源" : "新增 RSS 源";
  $("source-id").value = item?.id || "";
  $("source-name").value = item?.name || "";
  $("source-mode").value = item?.mode || "search";
  $("source-language").value = item?.language || "";
  $("source-url").value = item?.url_template || "";
  $("source-active").checked = item?.active ?? true;
  $("source-dialog").showModal();
}

function openKeywordDialog(item = null) {
  $("keyword-dialog-title").textContent = item ? "编辑关键词组" : "新增关键词组";
  $("keyword-id").value = item?.id || "";
  $("keyword-name").value = item?.name || "";
  $("keyword-terms").value = item?.match_terms.join("\n") || "";
  $("keyword-query").value = buildKeywordQuery(item?.match_terms || []);
  $("keyword-active").checked = item?.active ?? true;
  $("keyword-dialog").showModal();
}

function parseKeywordTerms() {
  return $("keyword-terms").value.split(/[\n,，]/).map((term) => term.trim()).filter(Boolean);
}

function buildKeywordQuery(terms) {
  const seen = new Set();
  const normalized = [];
  terms.forEach((value) => {
    const term = value.replaceAll('"', " ").trim().replace(/\s+/g, " ");
    const key = term.toLocaleLowerCase();
    if (term && !seen.has(key)) {
      seen.add(key);
      normalized.push(term);
    }
  });
  return normalized.length ? `(${normalized.map((term) => `"${term}"`).join(" OR ")})` : "";
}

function updateKeywordQueryPreview() {
  $("keyword-query").value = buildKeywordQuery(parseKeywordTerms());
}

async function saveSource(event) {
  event.preventDefault();
  const id = $("source-id").value;
  const payload = {
    name: $("source-name").value.trim(), mode: $("source-mode").value,
    language: $("source-language").value.trim(), url_template: $("source-url").value.trim(),
    active: $("source-active").checked,
  };
  try {
    await api(id ? `/api/sources/${id}` : "/api/sources", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
    $("source-dialog").close();
    await Promise.all([loadCatalogs(), loadStatus()]);
    showToast(id ? "RSS 源已更新" : "RSS 源已新增");
  } catch (error) { showToast(error.message, true); }
}

async function saveKeyword(event) {
  event.preventDefault();
  const id = $("keyword-id").value;
  const terms = parseKeywordTerms();
  const payload = { name: $("keyword-name").value.trim(), query: buildKeywordQuery(terms), match_terms: terms, active: $("keyword-active").checked };
  try {
    await api(id ? `/api/keywords/${id}` : "/api/keywords", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
    $("keyword-dialog").close();
    await Promise.all([loadCatalogs(), loadStatus()]);
    showToast(id ? "关键词组已更新" : "关键词组已新增");
  } catch (error) { showToast(error.message, true); }
}

async function updateSourceActive(id, active) {
  const item = state.sources.find((source) => source.id === Number(id));
  if (!item) return;
  await api(`/api/sources/${id}`, { method: "PUT", body: JSON.stringify({ name: item.name, url_template: item.url_template, mode: item.mode, language: item.language, active }) });
  await Promise.all([loadCatalogs(), loadStatus()]);
}

async function updateKeywordActive(id, active) {
  const item = state.keywords.find((keyword) => keyword.id === Number(id));
  if (!item) return;
  await api(`/api/keywords/${id}`, { method: "PUT", body: JSON.stringify({ name: item.name, query: item.query, match_terms: item.match_terms, active }) });
  await Promise.all([loadCatalogs(), loadStatus()]);
}

async function archiveItem(type, id) {
  const label = type === "sources" ? "RSS 源" : "关键词组";
  if (!window.confirm(`确定删除这个${label}吗？历史文章和日志仍会保留。`)) return;
  try {
    await api(`/api/${type}/${id}`, { method: "DELETE" });
    await Promise.all([loadCatalogs(), loadStatus()]);
    showToast(`${label}已删除`);
  } catch (error) { showToast(error.message, true); }
}

async function loadSettings() {
  try { const data = await api("/api/settings"); $("schedule-time").value = data.schedule_time; }
  catch (error) { showToast(error.message, true); }
}

async function saveSettings(event) {
  event.preventDefault();
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ schedule_time: $("schedule-time").value }) });
    await loadStatus();
    showToast("每日采集时间已保存");
  } catch (error) { showToast(error.message, true); }
}

function debounce(fn, delay) {
  let timer;
  return (...args) => { window.clearTimeout(timer); timer = window.setTimeout(() => fn(...args), delay); };
}

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $("collect-now").addEventListener("click", startCollection);
  $("refresh-articles").addEventListener("click", loadArticles);
  $("refresh-runs").addEventListener("click", loadRuns);
  $("article-search").addEventListener("input", debounce(() => { state.articleOffset = 0; loadArticles(); }, 300));
  $("article-source-filter").addEventListener("change", () => { state.articleOffset = 0; loadArticles(); });
  $("article-keyword-filter").addEventListener("change", () => { state.articleOffset = 0; loadArticles(); });
  $("article-prev").addEventListener("click", () => { state.articleOffset = Math.max(0, state.articleOffset - state.articleLimit); loadArticles(); });
  $("article-next").addEventListener("click", () => { state.articleOffset += state.articleLimit; loadArticles(); });
  $("add-source").addEventListener("click", () => openSourceDialog());
  $("add-keyword").addEventListener("click", () => openKeywordDialog());
  $("source-form").addEventListener("submit", saveSource);
  $("keyword-form").addEventListener("submit", saveKeyword);
  $("keyword-terms").addEventListener("input", updateKeywordQueryPreview);
  $("keyword-terms").addEventListener("compositionend", updateKeywordQueryPreview);
  $("settings-form").addEventListener("submit", saveSettings);
  document.querySelectorAll(".close-dialog").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));

  document.body.addEventListener("click", (event) => {
    const target = event.target.closest("button");
    if (!target) return;
    const id = target.dataset.id;
    if (target.classList.contains("run-detail")) openRunDetail(id);
    if (target.classList.contains("source-edit")) openSourceDialog(state.sources.find((item) => item.id === Number(id)));
    if (target.classList.contains("keyword-edit")) openKeywordDialog(state.keywords.find((item) => item.id === Number(id)));
    if (target.classList.contains("source-delete")) archiveItem("sources", id);
    if (target.classList.contains("keyword-delete")) archiveItem("keywords", id);
  });

  document.body.addEventListener("change", async (event) => {
    try {
      if (event.target.classList.contains("source-toggle")) await updateSourceActive(event.target.dataset.id, event.target.checked);
      if (event.target.classList.contains("keyword-toggle")) await updateKeywordActive(event.target.dataset.id, event.target.checked);
    } catch (error) { showToast(error.message, true); await loadCatalogs(); }
  });
}

async function initialize() {
  bindEvents();
  refreshIcons();
  try { await Promise.all([loadCatalogs(), loadStatus(), loadArticles(), loadRuns(), loadSettings()]); }
  catch (error) { showToast(error.message, true); }
  window.setInterval(loadStatus, 5000);
}

window.addEventListener("DOMContentLoaded", initialize);
