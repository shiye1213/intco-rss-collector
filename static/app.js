const state = {
  view: "articles",
  sources: [],
  keywords: [],
  keywordHitStats: { overall: null, categories: [], keywords: [] },
  keywordCategories: [],
  keywordCategoryId: "all",
  articleOffset: 0,
  articleLimit: 50,
  articleTotal: 0,
  aiOffset: 0,
  aiLimit: 50,
  aiTotal: 0,
  reviewOffset: 0,
  reviewLimit: 50,
  reviewTotal: 0,
  aiBatchSize: 20,
  categories: {},
  wasRunning: false,
  wasAnalysisRunning: false,
  wasReportRunning: false,
  pollInFlight: false,
  cleanupPreview: null,
  toastTimer: null,
  crawlerEnabled: false,
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

function formatPercent(value) {
  return value == null ? "-" : `${Math.round(Number(value) * 100)}%`;
}

function statusLabel(status) {
  const labels = { success: "成功", partial: "部分失败", failed: "失败", running: "运行中", interrupted: "已中断", pending: "待处理", processing: "处理中", skipped: "跳过", waiting: "等待重试", retry_ready: "可重试", final_failed: "最终失败", ignored: "已忽略" };
  return `<span class="status-chip status-${escapeHtml(status)}">${labels[status] || escapeHtml(status)}</span>`;
}

function triggerLabel(trigger) {
  return trigger === "scheduled" ? "定时" : "手动";
}

function aiTriggerLabel(trigger) {
  return trigger === "collection" ? "采集后自动" : "手动";
}

function categoryLabel(category) {
  return state.categories[category] || category || "-";
}

function riskMarkup(level, score) {
  const labels = { low: "低", medium: "中", high: "高", critical: "严重" };
  return `<span class="risk-chip risk-${escapeHtml(level)}">${labels[level] || escapeHtml(level)} · ${Number(score) || 0}</span>`;
}

function relevanceMarkup(relevant, score) {
  const label = relevant ? "真相关" : "审核无关";
  return `<span class="relevance-chip ${relevant ? "relevant" : "irrelevant"}">${label} · ${Number(score) || 0}</span>`;
}

function formatChars(value) {
  return `${Number(value || 0).toLocaleString("zh-CN")} 字符`;
}

function todayInShanghai() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date());
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function daysAgoInShanghai(days) {
  const target = new Date(Date.now() - days * 24 * 60 * 60 * 1000);
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(target);
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
  if (view === "intelligence") loadIntelligence();
  if (view === "reports") loadReports();
  if (view === "sources") renderSources();
  if (view === "keywords") renderKeywords();
  if (view === "settings") Promise.all([loadSettings(), loadAISettings()]);
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
  const [sourceData, keywordData, categoryData, hitStatsData] = await Promise.all([
    api("/api/sources"), api("/api/keywords"), api("/api/keyword-categories"),
    api("/api/keyword-hit-stats"),
  ]);
  state.sources = sourceData.items;
  state.keywords = keywordData.items;
  state.keywordCategories = categoryData.items;
  state.keywordHitStats = hitStatsData;
  fillFilters();
  renderSources();
  renderKeywords();
}

function fillFilters() {
  const sourceValue = $("article-source-filter").value;
  const keywordValue = $("article-keyword-filter").value;
  $("article-source-filter").innerHTML = `<option value="">全部数据源</option>${state.sources.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("")}`;
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
      const keywords = item.keywords?.map((keyword) => keyword.keyword_name) || (item.keyword_names || "").split(",").filter(Boolean);
      const sources = item.sources || [];
      const sourceNames = item.source_names?.length ? item.source_names : [item.feed_name].filter(Boolean);
      const metadata = [...(item.languages || []), ...(item.countries || []), ...(item.categories || []).slice(0, 3)];
      return `<tr>
        <td><a class="article-title" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a><span class="cell-subtitle">${escapeHtml(item.summary || "")}</span></td>
        <td>${escapeHtml(item.publisher_normalized || item.publisher || "-")}<span class="cell-subtitle">${escapeHtml(sourceNames.join(" / ") || "-")}${sources.length > 1 ? ` · ${sources.length} 个来源` : ""}</span><span class="cell-meta">${escapeHtml(metadata.join(" · "))}</span></td>
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
    const collectionRunSelect = $("ai-collection-run");
    const selectedRunId = collectionRunSelect.value;
    const selectableRuns = data.items.filter((run) => Number(run.items_inserted) > 0);
    collectionRunSelect.innerHTML = `<option value="">全部采集批次（全部待办）</option>${selectableRuns
      .map((run) => `<option value="${Number(run.id)}">采集 #${Number(run.id)} · ${escapeHtml(formatFullTime(run.started_at))} · 新增 ${Number(run.items_inserted)} 篇</option>`)
      .join("")}`;
    if (selectableRuns.some((run) => String(run.id) === selectedRunId)) collectionRunSelect.value = selectedRunId;
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

function fillCategoryOptions() {
  const select = $("ai-category-filter");
  const current = select.value;
  select.innerHTML = `<option value="">全部分类</option>${Object.entries(state.categories)
    .map(([code, label]) => `<option value="${escapeHtml(code)}">${escapeHtml(label)}</option>`)
    .join("")}`;
  select.value = current;
}

async function loadAIStatus() {
  try {
    const data = await api("/api/ai/status");
    state.categories = data.categories || {};
    fillCategoryOptions();
    $("ai-metric-pending").textContent = data.pending;
    $("ai-metric-content-ready").textContent = data.content_ready;
    $("ai-metric-content-failed").textContent = data.content_failed;
    $("ai-metric-retry-waiting").textContent = data.content_retry_waiting;
    $("ai-metric-final-failed").textContent = data.content_final_failed;
    $("ai-metric-ignored").textContent = data.content_ignored;
    $("ai-metric-relevant").textContent = data.relevant;
    $("ai-metric-irrelevant").textContent = data.irrelevant;
    $("ai-metric-analyzed").textContent = data.analyzed;
    $("ai-metric-threshold").textContent = data.relevance_threshold;
    const readerStatus = data.content_reader_configured
      ? data.content_reader_model
      : `${data.content_reader_model}（OPENAI_API_KEY 未配置）`;
    const analysisStatus = data.report_configured
      ? data.analysis_model
      : `${data.analysis_model}（DEEPSEEK_API_KEY 未配置）`;
    const taskStatus = data.analysis_running
      ? ` · 分析任务 #${data.analysis_run_id} ${data.analysis_pause_requested ? "正在暂停" : "运行中"}`
      : "";
    const statusText = `正文读取 ${readerStatus} · 内容分析 ${analysisStatus}${taskStatus}`;
    $("ai-view-status").textContent = statusText;
    $("ai-settings-status").textContent = statusText;
    const analyzeButton = $("analyze-pending");
    const collectionRunSelect = $("ai-collection-run");
    analyzeButton.disabled = !data.configured || data.analysis_running || data.pending === 0;
    analyzeButton.querySelector("span").textContent = data.analysis_running
      ? "正在处理"
      : collectionRunSelect.value ? "处理所选采集" : "处理全部待办";
    collectionRunSelect.disabled = data.analysis_running;
    const pauseButton = $("pause-analysis");
    pauseButton.classList.toggle("hidden", !data.analysis_running);
    pauseButton.disabled = data.analysis_pause_requested;
    pauseButton.querySelector("span").textContent = data.analysis_pause_requested ? "正在暂停" : "暂停处理";
    $("clear-pending").disabled = data.analysis_running || data.report_running || data.pending === 0;
    const reportButton = $("generate-report");
    reportButton.disabled = !data.report_configured || data.report_running;
    reportButton.querySelector("span").textContent = data.report_running ? "正在生成" : "生成日报";
    if (state.wasAnalysisRunning && !data.analysis_running) {
      await Promise.all([loadAIArticles(), loadAIReviews(), loadAIRuns(), loadContentFailures()]);
      showToast("AI 处理任务已结束");
    }
    if (state.wasReportRunning && !data.report_running) {
      await loadReports();
      showToast("情报日报生成任务已结束");
    }
    state.wasAnalysisRunning = data.analysis_running;
    state.wasReportRunning = data.report_running;
    refreshIcons();
  } catch (error) { showToast(error.message, true); }
}

async function pollLiveState() {
  if (state.pollInFlight) return;
  state.pollInFlight = true;
  try {
    await Promise.allSettled([loadStatus(), loadAIStatus()]);
    if (state.view === "intelligence" && state.wasAnalysisRunning) {
      await Promise.allSettled([loadContentFailures(), loadAIRuns()]);
    }
  } finally {
    state.pollInFlight = false;
  }
}

function contentFailureKindLabel(kind) {
  const labels = {
    network: "网络错误", dns: "域名解析", resolver: "Google News 解析",
    extraction: "正文抽取", content_type: "内容类型", too_large: "页面过大",
    invalid_url: "地址无效", unsafe_url: "地址安全", no_url: "无可用地址",
    http_429: "访问限流 (HTTP 429)", http_403: "访问受限 (HTTP 403)",
    llm_web_unavailable: "大模型网页读取不可用",
    llm_web_search_unavailable: "DeepSeek 网页搜索服务不可用",
    llm_no_web_evidence: "大模型未实际联网",
    llm_incomplete_content: "大模型正文不完整",
    llm_invalid_response: "大模型返回格式错误",
    llm_network: "DeepSeek 连接错误", llm_http_429: "DeepSeek 限流 (HTTP 429)",
    llm_migration_pending: "等待大模型重新读取",
    openai_not_configured: "CCTQ/OpenAI API Key 未配置",
    openai_web_unavailable: "GPT 网页读取不可用",
    openai_web_search_unavailable: "CCTQ/OpenAI 网页搜索服务不可用",
    openai_no_web_evidence: "GPT 未实际执行网页搜索",
    openai_incomplete_content: "GPT 返回正文不完整",
    openai_incomplete_response: "CCTQ/OpenAI 响应未完成",
    openai_invalid_response: "CCTQ/OpenAI 返回格式错误",
    openai_network: "CCTQ/OpenAI 连接错误",
    openai_http_429: "CCTQ/OpenAI 限流 (HTTP 429)",
    unexpected: "程序异常", unknown: "未分类",
  };
  return String(kind || "unknown").split("+").map((item) => labels[item] || item).join(" / ");
}

async function loadContentFailures() {
  try {
    const data = await api("/api/ai/content-failures?limit=100");
    $("content-failure-rows").innerHTML = data.items.map((item) => `<tr>
      <td><a class="article-title" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a><span class="cell-subtitle">${escapeHtml(item.publisher || "-")} · ${formatFullTime(item.published_at)}</span></td>
      <td>${statusLabel(item.disposition)}</td>
      <td>${escapeHtml(contentFailureKindLabel(item.failure_kind))}</td>
      <td>${item.attempt_count} / 3<span class="cell-subtitle">${item.next_retry_at ? `下次 ${formatFullTime(item.next_retry_at)}` : "不再自动重试"}</span></td>
      <td><span class="analysis-detail expanded">${escapeHtml(item.error_message || "-")}</span></td>
      <td><div class="row-actions"><button class="icon-button content-retry" data-id="${item.article_id}" type="button" title="立即重试"><i data-lucide="rotate-cw"></i></button><button class="icon-button danger-button content-ignore" data-id="${item.article_id}" type="button" title="忽略" ${item.disposition === "ignored" ? "disabled" : ""}><i data-lucide="circle-slash-2"></i></button></div></td>
    </tr>`).join("");
    $("content-failure-empty").classList.toggle("hidden", data.items.length > 0);
    refreshIcons();
  } catch (error) { showToast(error.message, true); }
}

async function retryContentFailure(articleId) {
  try {
    const data = await api(`/api/ai/content-failures/${articleId}/retry`, { method: "POST" });
    state.wasAnalysisRunning = true;
    showToast(`文章 #${articleId} 已启动重试，任务 #${data.run_id}`);
    await Promise.all([loadAIStatus(), loadContentFailures()]);
  } catch (error) { showToast(error.message, true); }
}

async function ignoreContentFailure(articleId) {
  if (!window.confirm("确定忽略这篇文章的全文读取失败吗？它将不再进入自动待办。")) return;
  try {
    await api(`/api/ai/content-failures/${articleId}/ignore`, { method: "POST" });
    showToast(`文章 #${articleId} 已忽略`);
    await Promise.all([loadAIStatus(), loadContentFailures()]);
  } catch (error) { showToast(error.message, true); }
}

async function loadAIArticles() {
  const params = new URLSearchParams({ limit: state.aiLimit, offset: state.aiOffset });
  const category = $("ai-category-filter").value;
  let dateFrom = $("ai-date-from").value;
  const dateTo = $("ai-date-to").value;
  if (!dateFrom && dateTo) dateFrom = dateTo;
  if (category) params.set("category", category);
  if (dateFrom) params.set("date_from", dateFrom);
  if (dateTo) params.set("date_to", dateTo);
  try {
    const data = await api(`/api/ai/articles?${params}`);
    state.aiTotal = data.total;
    $("ai-article-rows").innerHTML = data.items.map((item) => `<tr>
      <td><a class="article-title" href="${escapeHtml(item.final_url || item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a><span class="cell-subtitle">${escapeHtml(item.publisher || "-")} · ${formatFullTime(item.published_at)}</span><span class="cell-meta">全文 ${formatChars(item.content_chars)}</span></td>
      <td>${relevanceMarkup(true, item.relevance_score)}<span class="cell-meta">置信度 ${item.relevance_confidence}</span></td>
      <td><span class="tag">${escapeHtml(categoryLabel(item.category))}</span>${(item.secondary_categories || []).map((code) => `<span class="cell-meta">${escapeHtml(categoryLabel(code))}</span>`).join("")}</td>
      <td><strong class="analysis-summary">${escapeHtml(item.summary || "-")}</strong><span class="analysis-detail">${escapeHtml(item.impact_analysis || "-")}</span></td>
      <td>${riskMarkup(item.risk_level, item.risk_score)}<span class="cell-meta">影响 ${item.impact_score} / 5</span></td>
      <td>${formatFullTime(item.analyzed_at)}<span class="cell-subtitle">${escapeHtml(item.model)}</span></td>
    </tr>`).join("");
    $("ai-article-empty").classList.toggle("hidden", data.items.length > 0);
    const page = Math.floor(state.aiOffset / state.aiLimit) + 1;
    const pages = Math.max(1, Math.ceil(data.total / state.aiLimit));
    $("ai-page").textContent = `第 ${page} / ${pages} 页`;
    $("ai-prev").disabled = state.aiOffset === 0;
    $("ai-next").disabled = state.aiOffset + state.aiLimit >= data.total;
  } catch (error) { showToast(error.message, true); }
}

async function loadAIReviews() {
  const params = new URLSearchParams({ limit: state.reviewLimit, offset: state.reviewOffset });
  const relevant = $("ai-review-filter").value;
  let dateFrom = $("ai-date-from").value;
  const dateTo = $("ai-date-to").value;
  if (!dateFrom && dateTo) dateFrom = dateTo;
  if (relevant) params.set("relevant", relevant);
  if (dateFrom) params.set("date_from", dateFrom);
  if (dateTo) params.set("date_to", dateTo);
  try {
    const data = await api(`/api/ai/reviews?${params}`);
    state.reviewTotal = data.total;
    $("ai-review-rows").innerHTML = data.items.map((item) => `<tr>
      <td><a class="article-title" href="${escapeHtml(item.final_url || item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a><span class="cell-subtitle">${escapeHtml(item.publisher || "-")} · ${formatFullTime(item.published_at)}</span><span class="cell-meta">全文 ${formatChars(item.content_chars)}</span></td>
      <td>${relevanceMarkup(Boolean(item.is_relevant), item.relevance_score)}<span class="cell-meta">置信度 ${item.confidence}</span></td>
      <td><span class="tag">${escapeHtml(categoryLabel(item.category || "other"))}</span>${item.secondary_categories?.length ? `<span class="cell-meta">${item.secondary_categories.map(categoryLabel).map(escapeHtml).join("、")}</span>` : ""}</td>
      <td><span class="analysis-detail expanded">${escapeHtml(item.relevance_reason || "-")}</span></td>
      <td><span class="analysis-detail expanded">${escapeHtml((item.evidence || []).join("；") || "-")}</span></td>
      <td>${formatFullTime(item.reviewed_at)}<span class="cell-subtitle">${escapeHtml(item.model)}</span></td>
    </tr>`).join("");
    $("ai-review-empty").classList.toggle("hidden", data.items.length > 0);
    const page = Math.floor(state.reviewOffset / state.reviewLimit) + 1;
    const pages = Math.max(1, Math.ceil(data.total / state.reviewLimit));
    $("ai-review-page").textContent = `第 ${page} / ${pages} 页`;
    $("ai-review-prev").disabled = state.reviewOffset === 0;
    $("ai-review-next").disabled = state.reviewOffset + state.reviewLimit >= data.total;
  } catch (error) { showToast(error.message, true); }
}

async function loadAIRuns() {
  try {
    const data = await api("/api/ai/runs?limit=50");
    $("ai-run-rows").innerHTML = data.items.map((run) => `<tr>
      <td>#${run.id}</td><td>${aiTriggerLabel(run.trigger_type)}</td><td>${statusLabel(run.status)}</td>
      <td>${escapeHtml(run.model)}<span class="cell-subtitle">${escapeHtml(run.prompt_version)}</span></td>
      <td>${run.articles_succeeded} / ${run.articles_total}<span class="cell-subtitle">失败 ${run.articles_failed}</span></td>
      <td>${run.relevant_count} / ${run.irrelevant_count}</td>
      <td>${run.prompt_tokens + run.completion_tokens}<span class="cell-subtitle">输入 ${run.prompt_tokens} · 输出 ${run.completion_tokens}</span></td>
      <td>${formatFullTime(run.started_at)}</td>
      <td><button class="icon-button ai-run-detail" data-id="${run.id}" type="button" title="查看处理明细"><i data-lucide="list-tree"></i></button></td>
    </tr>`).join("");
    $("ai-run-empty").classList.toggle("hidden", data.items.length > 0);
  } catch (error) { showToast(error.message, true); }
}

async function loadIntelligence() {
  await Promise.all([loadAIStatus(), loadContentFailures(), loadAIArticles(), loadAIReviews(), loadAIRuns()]);
  refreshIcons();
}

async function startAIAnalysis() {
  const button = $("analyze-pending");
  const collectionRunId = Number($("ai-collection-run").value) || null;
  button.disabled = true;
  try {
    const data = await api("/api/ai/analyze", {
      method: "POST",
      body: JSON.stringify({
        limit: state.aiBatchSize, process_all: true, force: false,
        refresh_content: false, collection_run_id: collectionRunId,
      }),
    });
    state.wasAnalysisRunning = true;
    const scope = collectionRunId ? `采集 #${collectionRunId}` : "全部待办";
    showToast(`${scope}的 AI 处理已启动，共 ${data.article_count} 篇，将按每批 ${data.batch_size} 篇连续处理`);
    await loadAIStatus();
  } catch (error) {
    showToast(error.message, true);
    await loadAIStatus();
  }
}

async function pauseAIAnalysis() {
  const button = $("pause-analysis");
  button.disabled = true;
  try {
    await api("/api/ai/pause", { method: "POST" });
    showToast("已请求暂停，将在当前文章处理完成后停止");
    await loadAIStatus();
  } catch (error) {
    showToast(error.message, true);
    await loadAIStatus();
  }
}

async function clearPendingArticles() {
  const pending = Number($("ai-metric-pending").textContent || 0);
  if (!pending) return;
  if (!window.confirm(`确定删除当前全部 ${pending} 条待处理信息吗？已完成分析的信息不会删除。`)) return;
  const confirmation = window.prompt("此操作不可撤销。请输入 DELETE 再次确认：");
  if (confirmation === null) return;
  if (confirmation !== "DELETE") {
    showToast("确认文字不正确，未执行删除", true);
    return;
  }
  const button = $("clear-pending");
  button.disabled = true;
  try {
    const result = await api("/api/ai/pending/clear", {
      method: "POST",
      body: JSON.stringify({ confirmation }),
    });
    state.articleOffset = 0;
    showToast(`已删除 ${result.deleted} 条待处理信息`);
    await Promise.all([loadStatus(), loadArticles(), loadRuns(), loadIntelligence()]);
  } catch (error) {
    showToast(error.message, true);
    await loadAIStatus();
  }
}

async function loadReports() {
  try {
    const data = await api("/api/reports?limit=100");
    $("report-rows").innerHTML = data.items.map((report) => `<tr>
      <td><strong>${escapeHtml(report.title || `日报 #${report.id}`)}</strong><span class="cell-subtitle">#${report.id} · ${escapeHtml(report.model)}</span></td>
      <td>${escapeHtml(report.report_date)}</td><td>${report.article_count}</td>
      <td>${riskMarkup(report.risk_level, report.risk_score)}</td><td>${statusLabel(report.status)}</td>
      <td>${formatFullTime(report.updated_at)}</td>
      <td><div class="report-actions"><button class="icon-button report-detail-button" data-id="${report.id}" type="button" title="查看日报" ${report.status !== "success" ? "disabled" : ""}><i data-lucide="file-search"></i></button><button class="icon-button report-feishu-button" data-id="${report.id}" type="button" title="推送到飞书" ${report.status !== "success" ? "disabled" : ""}><i data-lucide="send"></i></button></div></td>
    </tr>`).join("");
    $("report-rows").querySelectorAll(".report-actions").forEach((actions, index) => {
      const report = data.items[index];
      actions.insertAdjacentHTML("beforeend", `<button class="icon-button danger-button report-delete-button" data-id="${report.id}" type="button" title="\u5220\u9664\u65e5\u62a5" ${report.status === "running" ? "disabled" : ""}><i data-lucide="trash-2"></i></button>`);
    });
    $("report-empty").classList.toggle("hidden", data.items.length > 0);
    refreshIcons();
  } catch (error) { showToast(error.message, true); }
}

async function sendReportToFeishu(id) {
  try {
    const data = await api(`/api/reports/${id}/feishu`, { method: "POST" });
    showToast(`日报 #${data.report_id} 已推送到飞书`);
  } catch (error) { showToast(error.message, true); }
}

async function deleteReport(id) {
  if (!window.confirm(`\u786e\u5b9a\u5220\u9664\u65e5\u62a5 #${id} \u5417\uff1f\u5220\u9664\u540e\u65e0\u6cd5\u6062\u590d\u3002`)) return;
  try {
    const data = await api(`/api/reports/${id}`, { method: "DELETE" });
    showToast(`\u65e5\u62a5 #${data.report_id} \u5df2\u5220\u9664`);
    await loadReports();
  } catch (error) { showToast(error.message, true); }
}

async function generateReport(event) {
  event.preventDefault();
  try {
    const data = await api("/api/reports", {
      method: "POST",
      body: JSON.stringify({ report_date: $("report-date").value }),
    });
    state.wasReportRunning = true;
    showToast(`日报 #${data.report_id} 已开始生成，共 ${data.article_count} 篇新闻`);
    await Promise.all([loadAIStatus(), loadReports()]);
  } catch (error) { showToast(error.message, true); }
}

function simplifiedReportSourceTitle(source, limit = 24) {
  const title = String(source?.title || source?.publisher || "原文").replace(/\s+/g, " ").trim();
  return title.length > limit ? `${title.slice(0, limit)}…` : title;
}

function reportSources(articleIds, articleById, attachedSources = []) {
  const sources = attachedSources.length
    ? attachedSources
    : [...new Set((articleIds || []).map(Number))]
      .map((articleId) => articleById.get(articleId))
      .filter(Boolean);
  if (!sources.length) return "";
  return `<div class="report-sources"><span>出处</span>${sources.map((article) => {
    const linkText = simplifiedReportSourceTitle(article);
    const linkTitle = [article.publisher, article.title].filter(Boolean).join(" · ") || linkText;
    return `<a href="${escapeHtml(article.source_url || article.final_url || article.url)}" title="${escapeHtml(linkTitle)}" target="_blank" rel="noopener noreferrer">${escapeHtml(linkText)}</a>`;
  }).join("")}</div>`;
}

function reportList(title, items, articleById) {
  if (!items?.length) return "";
  return `<section class="report-block"><h4>${escapeHtml(title)}</h4><ul class="cited-report-list">${items.map((item) => {
    const normalized = typeof item === "string"
      ? { category: "other", content: item, article_ids: [] }
      : item;
    return `<li><div class="report-item-content"><span class="tag">${escapeHtml(categoryLabel(normalized.category || "other"))}</span><span>${escapeHtml(normalized.content || "")}</span></div>${reportSources(normalized.article_ids, articleById, normalized.sources)}</li>`;
  }).join("")}</ul></section>`;
}

function reportImpactMarkup(level, score) {
  const labels = { low: "低", medium: "中", high: "高", critical: "严重" };
  const stars = Math.max(1, Math.min(5, Math.ceil((Number(score) || 0) / 20)));
  return `<span class="report-impact-stars" aria-label="${escapeHtml(labels[level] || level)}">${"★".repeat(stars)}${"☆".repeat(5 - stars)}</span><span>（${escapeHtml(labels[level] || level || "低")}）</span>`;
}

function reportNewsAnalysis(report, articleById) {
  const developments = new Map(
    (report.key_developments || [])
      .filter((item) => item && typeof item === "object")
      .map((item) => [Number(item.article_id), item]),
  );
  const articles = report.articles || [];
  if (!articles.length) return "";
  return `<section class="report-block report-news-analysis"><h4>逐条新闻分析</h4><div class="report-news-list">${articles.map((article, index) => {
    const item = developments.get(Number(article.article_id)) || {};
    const hasDetailedFormat = Boolean(item.affected_region || item.products || item.impact_reason || item.recommended_action);
    const title = hasDetailedFormat ? (item.title || article.title) : article.title;
    const category = item.category || article.category || "other";
    const level = item.risk_level || article.risk_level || "low";
    const score = Number(item.risk_score ?? article.risk_score) || 0;
    const riskFactors = Array.isArray(article.risk_factors) ? article.risk_factors.filter(Boolean).join("；") : "";
    const actions = Array.isArray(article.recommended_actions) ? article.recommended_actions.filter(Boolean).join("；") : "";
    return `<article class="report-news-card">
      <div class="report-news-title"><span>新闻 ${index + 1}</span><h5>${escapeHtml(title || "未命名新闻")}</h5></div>
      <div class="report-news-fields">
        <p><strong>新闻类型：</strong>${escapeHtml(categoryLabel(category))}</p>
        <p><strong>影响地区：</strong>${escapeHtml(item.affected_region || "暂未明确")}</p>
        <p><strong>涉及产品：</strong>${escapeHtml(item.products || "暂未明确")}</p>
        <p><strong>影响等级：</strong>${reportImpactMarkup(level, score)}</p>
      </div>
      <p><strong>核心事实：</strong>${escapeHtml(item.finding || article.summary || "暂未明确")}</p>
      <p><strong>影响原因：</strong>${escapeHtml(item.impact_reason || riskFactors || "暂未明确")}</p>
      <p><strong>业务影响：</strong>${escapeHtml(item.business_impact || article.impact_analysis || "暂未明确")}</p>
      <p><strong>建议措施：</strong>${escapeHtml(item.recommended_action || actions || "暂未明确")}</p>
      ${reportSources([article.article_id], articleById, item.sources)}
    </article>`;
  }).join("")}</div></section>`;
}

async function openReportDetail(id) {
  try {
    const report = await api(`/api/reports/${id}`);
    $("report-dialog-title").textContent = report.title || `情报日报 #${report.id}`;
    const articleById = new Map((report.articles || []).map((article) => [Number(article.article_id), article]));
    const allSourceIds = (report.articles || []).map((article) => article.article_id);
    $("report-detail").innerHTML = `
      <div class="report-meta"><span>${escapeHtml(report.report_date)}</span><span>${report.article_count} 篇新闻</span>${riskMarkup(report.risk_level, report.risk_score)}</div>
      <section class="report-lead"><h4>今日总体总结</h4><p>${escapeHtml(report.executive_summary)}</p><p class="risk-basis"><strong>整体风险依据：</strong>${escapeHtml(report.risk_basis)}</p>${reportSources(allSourceIds, articleById, report.sources)}</section>
      ${reportNewsAnalysis(report, articleById)}
      ${reportList("关键风险", report.key_risks, articleById)}
      ${reportList("业务机会", report.opportunities, articleById)}
      ${reportList("建议行动", report.recommended_actions, articleById)}
      ${reportList("后续监控", report.watchlist, articleById)}
      ${report.articles?.length ? `<section class="report-block"><h4>全部来源文章</h4><div class="report-article-list">${report.articles.map((article) => `<a href="${escapeHtml(article.final_url || article.url)}" target="_blank" rel="noopener noreferrer"><span><strong>${escapeHtml(article.title)}</strong><small>${escapeHtml(article.publisher || "-")} · ${formatFullTime(article.published_at)}</small></span>${riskMarkup(article.risk_level, article.risk_score)}</a>`).join("")}</div></section>` : ""}`;
    $("report-dialog").showModal();
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

async function openAIRunDetail(id) {
  try {
    const run = await api(`/api/ai/runs/${id}`);
    $("ai-run-dialog-title").textContent = `AI 处理日志 #${run.id}`;
    $("ai-run-summary").innerHTML = [
      ["状态", statusLabel(run.status)], ["文章", run.articles_total],
      ["成功", run.articles_succeeded], ["失败", run.articles_failed],
      ["Token", run.prompt_tokens + run.completion_tokens],
    ].map(([label, value]) => `<div><span>${label}</span><strong>${value}</strong></div>`).join("");
    $("ai-run-detail-rows").innerHTML = run.items.map((item) => {
      const reviewed = item.relevance_status === "success";
      return `<tr>
        <td><a class="article-title" href="${escapeHtml(item.final_url || item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a><span class="cell-meta">${item.content_chars ? formatChars(item.content_chars) : "尚无正文"}</span></td>
        <td>${statusLabel(item.content_status)}</td>
        <td>${statusLabel(item.relevance_status)}</td>
        <td>${statusLabel(item.business_analysis_status)}</td>
        <td>${reviewed ? relevanceMarkup(Boolean(item.is_relevant), item.relevance_score) : "-"}${item.category ? `<span class="cell-meta">${escapeHtml(categoryLabel(item.category))}</span>` : ""}</td>
        <td><span class="analysis-detail expanded">${escapeHtml(item.error_message || "-")}</span></td>
      </tr>`;
    }).join("");
    $("ai-run-dialog").showModal();
    refreshIcons();
  } catch (error) { showToast(error.message, true); }
}

function toggleMarkup(type, item) {
  return `<label class="toggle" title="${item.active ? "停用" : "启用"}"><input class="${type}-toggle" data-id="${item.id}" type="checkbox" ${item.active ? "checked" : ""} /><span class="toggle-track"></span></label>`;
}

function sourceModeLabel(mode) {
  return {
    search: "搜索型 RSS",
    direct: "直连 RSS",
    crawler: "网页爬虫",
  }[mode] || mode;
}

function sourceFallbackMarkup(mode) {
  if (!state.crawlerEnabled) {
    const detail = mode === "crawler"
      ? "开启系统总开关后采集"
      : "仍会正常读取 RSS";
    return `<span class="source-strategy"><strong>网页爬虫已关闭</strong><small>${detail}</small></span>`;
  }
  if (mode === "crawler") {
    return `<span class="source-strategy source-strategy-direct"><strong>直接网页爬虫</strong><small>此源不依赖 RSS</small></span>`;
  }
  return `<span class="source-strategy"><strong>RSS 优先 · 自动兜底</strong><small>返回 HTML 时切换爬虫</small></span>`;
}

function sourceCrawlerHealthMarkup(item) {
  const labels = {
    robots_denied: "robots.txt 禁止",
    rate_limited: "触发限流",
    access_blocked: "访问受限",
    temporarily_unavailable: "站点异常",
    extraction_failed: "解析失败",
  };
  if (!state.crawlerEnabled) {
    return `<span class="crawler-health"><strong>全局关闭</strong><small>未执行网页爬取</small></span>`;
  }
  if (item.crawler_in_cooldown) {
    return `<span class="crawler-health crawler-health-cooldown"><strong>冷却中</strong><small>${escapeHtml(labels[item.crawler_failure_kind] || item.crawler_failure_kind || "爬虫受限")} · 至 ${formatFullTime(item.crawler_cooldown_until)}</small></span>`;
  }
  if (item.crawler_last_success_at) {
    return `<span class="crawler-health crawler-health-ok"><strong>正常</strong><small>上次成功 ${formatTime(item.crawler_last_success_at)}</small></span>`;
  }
  if (item.crawler_failure_kind) {
    return `<span class="crawler-health crawler-health-warning"><strong>待重试</strong><small>${escapeHtml(labels[item.crawler_failure_kind] || item.crawler_failure_kind)}</small></span>`;
  }
  return `<span class="crawler-health"><strong>未检测</strong><small>尚无爬虫运行记录</small></span>`;
}

function renderSources() {
  $("source-rows").innerHTML = state.sources.map((item) => `<tr>
    <td><strong>${escapeHtml(item.name)}</strong></td>
    <td><span class="tag">${escapeHtml(sourceModeLabel(item.mode))}</span></td>
    <td>${sourceFallbackMarkup(item.mode)}</td>
    <td title="${escapeHtml(item.crawler_last_error || "")}">${sourceCrawlerHealthMarkup(item)}</td>
    <td>${escapeHtml(item.language || "-")}</td><td>${escapeHtml(item.country || "-")}</td><td class="url-cell" title="${escapeHtml(item.site_domain || "")}">${escapeHtml(item.site_domain || "-")}</td><td class="url-cell" title="${escapeHtml(item.url_template)}">${escapeHtml(item.url_template)}</td>
    <td>${toggleMarkup("source", item)}</td>
    <td><div class="row-actions"><button class="icon-button source-edit" data-id="${item.id}" title="编辑" type="button"><i data-lucide="pencil"></i></button><button class="icon-button danger-button source-delete" data-id="${item.id}" title="删除" type="button"><i data-lucide="trash-2"></i></button></div></td>
  </tr>`).join("");
  refreshIcons();
}

function renderKeywords() {
  const validCategories = new Set(state.keywordCategories.map((item) => String(item.id)));
  if (!["all", "unclassified"].includes(state.keywordCategoryId) && !validCategories.has(state.keywordCategoryId)) {
    state.keywordCategoryId = "all";
  }
  const emptyStats = {
    keyword_count: 0,
    hit_count: 0,
    reviewed_count: 0,
    business_relevant_count: 0,
    relevant_count: 0,
    pending_review_count: 0,
    hit_rate: null,
  };
  const categoryStatsById = new Map(
    state.keywordHitStats.categories.map((item) => [
      item.category_id == null ? "unclassified" : String(item.category_id),
      item,
    ]),
  );
  const keywordStatsById = new Map(
    state.keywordHitStats.keywords.map((item) => [Number(item.keyword_id), item]),
  );
  const menuItems = [
    { id: "all", name: "全部", stats: state.keywordHitStats.overall || emptyStats },
    ...state.keywordCategories.map((category) => ({
      id: String(category.id),
      name: category.name,
      stats: categoryStatsById.get(String(category.id)) || emptyStats,
    })),
    { id: "unclassified", name: "未分类", stats: categoryStatsById.get("unclassified") || emptyStats },
  ];
  $("keyword-category-menu").innerHTML = menuItems.map((item) => `<button class="button secondary keyword-category-button${state.keywordCategoryId === item.id ? " active" : ""}" data-category-id="${escapeHtml(item.id)}" type="button">${escapeHtml(item.name)} · 相关 ${item.stats.relevant_count} · ${formatPercent(item.stats.hit_rate)}</button>`).join("");
  const selectedStats = menuItems.find((item) => item.id === state.keywordCategoryId)?.stats || emptyStats;
  const summaryMetrics = [
    ["关键词组", selectedStats.keyword_count, "当前范围内未归档的关键词组"],
    ["候选命中", selectedStats.hit_count, "当前启用关键词初筛命中的去重文章"],
    ["已审核", selectedStats.reviewed_count, "已完成 AI 相关性判断"],
    ["业务相关", selectedStats.business_relevant_count, "通过企业业务边界审核"],
    ["分类相关", selectedStats.relevant_count, "业务相关且主/次分类匹配当前关键词分类"],
    ["命中率", formatPercent(selectedStats.hit_rate), "分类相关 ÷ 已审核"],
    ["待审核", selectedStats.pending_review_count, "尚不能计入命中率"],
  ];
  $("keyword-hit-summary").innerHTML = summaryMetrics.map(([label, value, hint]) => `<div class="keyword-hit-metric"><span>${label}</span><strong>${value}</strong><small>${hint}</small></div>`).join("");
  const visibleKeywords = state.keywords.filter((item) => {
    if (state.keywordCategoryId === "all") return true;
    if (state.keywordCategoryId === "unclassified") return item.category_id == null;
    return Number(item.category_id) === Number(state.keywordCategoryId);
  });
  $("keyword-rows").innerHTML = visibleKeywords.map((item) => {
    const stats = keywordStatsById.get(Number(item.id)) || emptyStats;
    return `<tr>
    <td><strong>${escapeHtml(item.name)}</strong></td>
    <td><span class="tag">${escapeHtml(item.category_name || "未分类")}</span></td>
    <td class="url-cell" title="${escapeHtml(item.query)}">${escapeHtml(item.query)}</td>
    <td><div class="keyword-list">${item.match_terms.slice(0, 5).map((term) => `<span class="tag">${escapeHtml(term)}</span>`).join("")}${item.match_terms.length > 5 ? `<span class="tag">+${item.match_terms.length - 5}</span>` : ""}</div></td>
    <td><span class="cell-subtitle">信号 ${item.context_terms.length} 个 · 排除 ${item.exclude_terms.length} 个</span><span class="cell-meta">回溯 ${item.lookback_days} 天${item.require_local_match ? " · 本地主题校验" : ""}</span></td>
    <td><strong>${stats.hit_count}</strong><span class="cell-subtitle">待审核 ${stats.pending_review_count}</span></td>
    <td><strong>${stats.relevant_count}</strong><span class="cell-subtitle">已审核 ${stats.reviewed_count}</span></td>
    <td><strong>${formatPercent(stats.hit_rate)}</strong><span class="cell-subtitle">相关 / 已审核</span></td>
    <td>${toggleMarkup("keyword", item)}</td>
    <td><div class="row-actions"><button class="icon-button keyword-edit" data-id="${item.id}" title="编辑" type="button"><i data-lucide="pencil"></i></button><button class="icon-button danger-button keyword-delete" data-id="${item.id}" title="删除" type="button"><i data-lucide="trash-2"></i></button></div></td>
  </tr>`;
  }).join("");
  $("keyword-empty").classList.toggle("hidden", visibleKeywords.length > 0);
  refreshIcons();
}

function syncSourceSiteField() {
  const siteInput = $("source-site-domain");
  const mode = $("source-mode").value;
  const isSearch = mode === "search";
  if (!isSearch) siteInput.value = "";
  siteInput.disabled = !isSearch;
  $("source-url-label").textContent = mode === "crawler" ? "新闻列表页地址" : "Feed 地址";
  $("source-url").placeholder = mode === "search"
    ? "https://news.google.com/rss/search?q={query}&hl=...；{query} 会由系统替换"
    : mode === "crawler"
      ? "https://example.com/news/；系统会抓取同站文章详情"
      : "https://example.com/feed.xml";
  $("source-url-hint").textContent = mode === "crawler"
    ? "最多抓取 30 篇同站文章；必须能从页面或结构化数据识别发布日期。"
    : mode === "search"
      ? "搜索型 RSS 地址必须包含 {query}；采集时系统会替换为关键词查询。"
      : "直连 RSS 必须填写固定 RSS/Atom 地址，不能包含 {query}。";
}

function handleSourceModeChange() {
  const modeSelect = $("source-mode");
  const previousMode = modeSelect.dataset.previousMode || "";
  const mode = modeSelect.value;
  const urlInput = $("source-url");
  const urlMatchesMode = mode === "search" && urlInput.value.includes("{query}");
  if (previousMode && previousMode !== mode
      && urlInput.value.trim() && !urlMatchesMode) {
    urlInput.value = "";
    showToast("数据源类型已切换，请填写与新类型匹配的地址");
  }
  modeSelect.dataset.previousMode = mode;
  syncSourceSiteField();
  urlInput.focus();
}

function openSourceDialog(item = null) {
  $("source-dialog-title").textContent = item ? "编辑数据源" : "新增数据源";
  $("source-id").value = item?.id || "";
  $("source-name").value = item?.name || "";
  $("source-mode").value = item?.mode || "search";
  $("source-language").value = item?.language || "";
  $("source-country").value = item?.country || "";
  $("source-site-domain").value = item?.site_domain || "";
  $("source-url").value = item?.url_template || "";
  $("source-active").checked = item?.active ?? true;
  $("source-mode").dataset.previousMode = $("source-mode").value;
  syncSourceSiteField();
  $("source-dialog").showModal();
}

function openKeywordDialog(item = null) {
  $("keyword-dialog-title").textContent = item ? "编辑关键词组" : "新增关键词组";
  $("keyword-id").value = item?.id || "";
  $("keyword-name").value = item?.name || "";
  $("keyword-category").innerHTML = `<option value="">未分类</option>${state.keywordCategories.map((category) => `<option value="${category.id}">${escapeHtml(category.name)}</option>`).join("")}`;
  const selectedCategory = item
    ? (item.category_id == null ? "" : String(item.category_id))
    : (/^\d+$/.test(state.keywordCategoryId) ? state.keywordCategoryId : String(state.keywordCategories[0]?.id || ""));
  $("keyword-category").value = selectedCategory;
  $("keyword-terms").value = item?.match_terms.join("\n") || "";
  $("keyword-context-terms").value = item?.context_terms.join("\n") || "";
  $("keyword-exclude-terms").value = item?.exclude_terms.join("\n") || "";
  $("keyword-lookback-days").value = item?.lookback_days || 30;
  $("keyword-query").value = item?.query || "";
  $("keyword-require-local-match").checked = Boolean(item?.require_local_match);
  $("keyword-active").checked = item?.active ?? true;
  updateKeywordQueryPreview();
  $("keyword-dialog").showModal();
}

function parseKeywordTerms(id) {
  return $(id).value.split(/[\n,，]/).map((term) => term.trim()).filter(Boolean);
}

function keywordStrategyPayload() {
  return {
    match_terms: parseKeywordTerms("keyword-terms"),
    context_terms: parseKeywordTerms("keyword-context-terms"),
    exclude_terms: parseKeywordTerms("keyword-exclude-terms"),
    lookback_days: Number($("keyword-lookback-days").value || 30),
  };
}

let keywordPreviewRevision = 0;

async function updateKeywordQueryPreview() {
  const payload = keywordStrategyPayload();
  const revision = ++keywordPreviewRevision;
  if (!payload.match_terms.length) {
    $("keyword-query").value = "";
    return;
  }
  try {
    const data = await api("/api/keywords/preview", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (revision === keywordPreviewRevision) $("keyword-query").value = data.query;
  } catch (error) {
    if (revision === keywordPreviewRevision) $("keyword-query").value = error.message;
  }
}

async function saveSource(event) {
  event.preventDefault();
  const id = $("source-id").value;
  const payload = {
    name: $("source-name").value.trim(), mode: $("source-mode").value,
    language: $("source-language").value.trim(), country: $("source-country").value.trim().toUpperCase(), url_template: $("source-url").value.trim(),
    site_domain: $("source-site-domain").value.trim(), active: $("source-active").checked,
  };
  try {
    await api(id ? `/api/sources/${id}` : "/api/sources", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
    $("source-dialog").close();
    await Promise.all([loadCatalogs(), loadStatus()]);
    showToast(id ? "数据源已更新" : "数据源已新增");
  } catch (error) { showToast(error.message, true); }
}

async function saveKeyword(event) {
  event.preventDefault();
  const id = $("keyword-id").value;
  const payload = {
    name: $("keyword-name").value.trim(),
    category_id: $("keyword-category").value ? Number($("keyword-category").value) : null,
    ...keywordStrategyPayload(),
    require_local_match: $("keyword-require-local-match").checked,
    active: $("keyword-active").checked,
  };
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
  await api(`/api/sources/${id}`, { method: "PUT", body: JSON.stringify({ name: item.name, url_template: item.url_template, mode: item.mode, language: item.language, country: item.country || "", site_domain: item.site_domain || "", active }) });
  await Promise.all([loadCatalogs(), loadStatus()]);
}

async function updateKeywordActive(id, active) {
  const item = state.keywords.find((keyword) => keyword.id === Number(id));
  if (!item) return;
  await api(`/api/keywords/${id}`, { method: "PUT", body: JSON.stringify({
    name: item.name,
    category_id: item.category_id,
    match_terms: item.match_terms,
    context_terms: item.context_terms,
    exclude_terms: item.exclude_terms,
    lookback_days: item.lookback_days,
    require_local_match: Boolean(item.require_local_match),
    active,
  }) });
  await Promise.all([loadCatalogs(), loadStatus()]);
}

async function archiveItem(type, id) {
  const label = type === "sources" ? "数据源" : "关键词组";
  if (!window.confirm(`确定删除这个${label}吗？历史文章和日志仍会保留。`)) return;
  try {
    await api(`/api/${type}/${id}`, { method: "DELETE" });
    await Promise.all([loadCatalogs(), loadStatus()]);
    showToast(`${label}已删除`);
  } catch (error) { showToast(error.message, true); }
}

async function loadSettings() {
  try {
    const data = await api("/api/settings");
    $("schedule-time").value = data.schedule_time;
    $("incremental-collection").checked = data.incremental_collection;
    $("search-local-keyword-filter").checked = data.search_local_keyword_filter;
    state.crawlerEnabled = data.crawler_enabled;
    $("crawler-enabled").checked = data.crawler_enabled;
    $("crawler-respect-robots").checked = data.crawler_respect_robots;
    $("crawler-min-interval").value = data.crawler_min_interval_seconds;
    $("crawler-cooldown-minutes").value = data.crawler_cooldown_minutes;
    syncCrawlerSettingsAvailability();
  } catch (error) { showToast(error.message, true); }
}

function syncCrawlerSettingsAvailability() {
  const enabled = $("crawler-enabled").checked;
  state.crawlerEnabled = enabled;
  ["crawler-respect-robots", "crawler-min-interval", "crawler-cooldown-minutes"]
    .forEach((id) => { $(id).disabled = !enabled; });
  const status = $("crawler-fallback-status").querySelector("span");
  status.textContent = enabled ? "网页爬虫兜底已启用" : "网页爬虫兜底已关闭";
  $("crawler-fallback-description").textContent = enabled
    ? "RSS 不可用时，采集任务自动切换到同站网页爬虫"
    : "开启总开关后，RSS 不可用时才会切换到同站网页爬虫";
  renderSources();
}

async function saveSettings(event) {
  event.preventDefault();
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        schedule_time: $("schedule-time").value,
        incremental_collection: $("incremental-collection").checked,
        search_local_keyword_filter: $("search-local-keyword-filter").checked,
        crawler_enabled: $("crawler-enabled").checked,
        crawler_respect_robots: $("crawler-respect-robots").checked,
        crawler_min_interval_seconds: Number($("crawler-min-interval").value),
        crawler_cooldown_minutes: Number($("crawler-cooldown-minutes").value),
      }),
    });
    await loadStatus();
    showToast("采集设置已保存");
  } catch (error) { showToast(error.message, true); }
}

async function loadAISettings() {
  try {
    const data = await api("/api/ai/settings");
    $("ai-business-profile").value = data.business_profile;
    $("ai-relevance-prompt").value = data.relevance_prompt;
    $("ai-report-prompt").value = data.report_prompt;
    $("ai-threshold").value = data.relevance_threshold;
    $("ai-batch-size").value = data.batch_size;
    $("ai-parallelism").value = data.parallelism;
    $("ai-content-max-chars").value = data.content_max_chars;
    $("ai-auto-analyze").checked = data.auto_analyze;
    $("ai-auto-report").checked = data.auto_report;
    $("ai-auto-feishu-push").checked = data.auto_feishu_push;
    state.aiBatchSize = data.batch_size;
  } catch (error) { showToast(error.message, true); }
}

async function saveAISettings(event) {
  event.preventDefault();
  const payload = {
    business_profile: $("ai-business-profile").value.trim(),
    relevance_prompt: $("ai-relevance-prompt").value.trim(),
    report_prompt: $("ai-report-prompt").value.trim(),
    relevance_threshold: Number($("ai-threshold").value),
    batch_size: Number($("ai-batch-size").value),
    parallelism: Number($("ai-parallelism").value),
    content_max_chars: Number($("ai-content-max-chars").value),
    auto_analyze: $("ai-auto-analyze").checked,
    auto_report: $("ai-auto-report").checked,
    auto_feishu_push: $("ai-auto-feishu-push").checked,
  };
  try {
    const data = await api("/api/ai/settings", { method: "PUT", body: JSON.stringify(payload) });
    state.aiBatchSize = data.batch_size;
    await loadAIStatus();
    showToast("AI 分析设置已保存");
  } catch (error) { showToast(error.message, true); }
}

function cleanupQuery() {
  const scope = $("cleanup-scope").value;
  const before = scope === "history" ? $("cleanup-before").value : "";
  if (scope === "history" && !before) throw new Error("请选择历史数据截止日期");
  const params = new URLSearchParams({ scope });
  if (before) params.set("before", before);
  return { scope, before, params };
}

function renderCleanupPreview(data) {
  state.cleanupPreview = data;
  $("cleanup-preview").textContent = `预计删除：文章 ${data.articles} 条、采集日志 ${data.collection_runs} 条、AI 日志 ${data.ai_analysis_runs} 条、日报 ${data.daily_reports} 条。`;
}

async function previewCleanup() {
  try {
    const { params } = cleanupQuery();
    const data = await api(`/api/maintenance/cleanup-preview?${params}`);
    renderCleanupPreview(data);
  } catch (error) { showToast(error.message, true); }
}

function syncCleanupFields() {
  const isHistory = $("cleanup-scope").value === "history";
  $("cleanup-before-label").classList.toggle("hidden", !isHistory);
  $("cleanup-before").required = isHistory;
  state.cleanupPreview = null;
  $("cleanup-preview").textContent = "尚未预览";
}

async function executeCleanup(event) {
  event.preventDefault();
  try {
    const { scope, before, params } = cleanupQuery();
    const preview = await api(`/api/maintenance/cleanup-preview?${params}`);
    renderCleanupPreview(preview);
    if (preview.total_records === 0) {
      showToast("当前范围没有可清理的数据");
      return;
    }
    const confirmation = window.prompt(
      `将删除 ${preview.total_records} 条主记录，并先自动备份数据库。请输入 DELETE 确认：`,
    );
    if (confirmation === null) return;
    const result = await api("/api/maintenance/cleanup", {
      method: "POST",
      body: JSON.stringify({ scope, before: before || null, confirmation }),
    });
    showToast(`清理完成，备份：${result.backup_path}`);
    state.cleanupPreview = null;
    $("cleanup-preview").textContent = `清理完成；备份文件：${result.backup_path}`;
    await Promise.all([loadStatus(), loadArticles(), loadRuns(), loadIntelligence(), loadReports()]);
  } catch (error) { showToast(error.message, true); }
}

function debounce(fn, delay) {
  let timer;
  return (...args) => { window.clearTimeout(timer); timer = window.setTimeout(() => fn(...args), delay); };
}

function bindEvents() {
  const refreshKeywordPreview = debounce(updateKeywordQueryPreview, 200);
  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $("collect-now").addEventListener("click", startCollection);
  $("refresh-articles").addEventListener("click", loadArticles);
  $("refresh-runs").addEventListener("click", loadRuns);
  $("analyze-pending").addEventListener("click", startAIAnalysis);
  $("pause-analysis").addEventListener("click", pauseAIAnalysis);
  $("clear-pending").addEventListener("click", clearPendingArticles);
  $("ai-collection-run").addEventListener("change", loadAIStatus);
  $("refresh-ai").addEventListener("click", loadIntelligence);
  $("refresh-content-failures").addEventListener("click", loadContentFailures);
  $("ai-category-filter").addEventListener("change", () => { state.aiOffset = 0; loadAIArticles(); });
  $("ai-date-from").addEventListener("change", () => { state.aiOffset = 0; state.reviewOffset = 0; Promise.all([loadAIArticles(), loadAIReviews()]); });
  $("ai-date-to").addEventListener("change", () => { state.aiOffset = 0; state.reviewOffset = 0; Promise.all([loadAIArticles(), loadAIReviews()]); });
  $("ai-prev").addEventListener("click", () => { state.aiOffset = Math.max(0, state.aiOffset - state.aiLimit); loadAIArticles(); });
  $("ai-next").addEventListener("click", () => { state.aiOffset += state.aiLimit; loadAIArticles(); });
  $("ai-review-filter").addEventListener("change", () => { state.reviewOffset = 0; loadAIReviews(); });
  $("ai-review-prev").addEventListener("click", () => { state.reviewOffset = Math.max(0, state.reviewOffset - state.reviewLimit); loadAIReviews(); });
  $("ai-review-next").addEventListener("click", () => { state.reviewOffset += state.reviewLimit; loadAIReviews(); });
  $("report-form").addEventListener("submit", generateReport);
  $("refresh-reports").addEventListener("click", loadReports);
  $("article-search").addEventListener("input", debounce(() => { state.articleOffset = 0; loadArticles(); }, 300));
  $("article-source-filter").addEventListener("change", () => { state.articleOffset = 0; loadArticles(); });
  $("article-keyword-filter").addEventListener("change", () => { state.articleOffset = 0; loadArticles(); });
  $("article-prev").addEventListener("click", () => { state.articleOffset = Math.max(0, state.articleOffset - state.articleLimit); loadArticles(); });
  $("article-next").addEventListener("click", () => { state.articleOffset += state.articleLimit; loadArticles(); });
  $("add-source").addEventListener("click", () => openSourceDialog());
  $("source-mode").addEventListener("change", handleSourceModeChange);
  $("add-keyword").addEventListener("click", () => openKeywordDialog());
  $("source-form").addEventListener("submit", saveSource);
  $("keyword-form").addEventListener("submit", saveKeyword);
  ["keyword-terms", "keyword-context-terms", "keyword-exclude-terms", "keyword-lookback-days"]
    .forEach((id) => $(id).addEventListener("input", refreshKeywordPreview));
  $("keyword-terms").addEventListener("compositionend", updateKeywordQueryPreview);
  $("keyword-context-terms").addEventListener("compositionend", updateKeywordQueryPreview);
  $("keyword-exclude-terms").addEventListener("compositionend", updateKeywordQueryPreview);
  $("settings-form").addEventListener("submit", saveSettings);
  $("crawler-enabled").addEventListener("change", syncCrawlerSettingsAvailability);
  $("ai-settings-form").addEventListener("submit", saveAISettings);
  $("cleanup-scope").addEventListener("change", syncCleanupFields);
  $("preview-cleanup").addEventListener("click", previewCleanup);
  $("cleanup-form").addEventListener("submit", executeCleanup);
  document.querySelectorAll(".close-dialog").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));

  document.body.addEventListener("click", (event) => {
    const target = event.target.closest("button");
    if (!target) return;
    const id = target.dataset.id;
    if (target.classList.contains("run-detail")) openRunDetail(id);
    if (target.classList.contains("ai-run-detail")) openAIRunDetail(id);
    if (target.classList.contains("report-detail-button")) openReportDetail(id);
    if (target.classList.contains("report-feishu-button")) sendReportToFeishu(id);
    if (target.classList.contains("source-edit")) openSourceDialog(state.sources.find((item) => item.id === Number(id)));
    if (target.classList.contains("report-delete-button")) deleteReport(id);
    if (target.classList.contains("keyword-edit")) openKeywordDialog(state.keywords.find((item) => item.id === Number(id)));
    if (target.classList.contains("keyword-category-button")) {
      state.keywordCategoryId = target.dataset.categoryId;
      renderKeywords();
    }
    if (target.classList.contains("source-delete")) archiveItem("sources", id);
    if (target.classList.contains("keyword-delete")) archiveItem("keywords", id);
    if (target.classList.contains("content-retry")) retryContentFailure(id);
    if (target.classList.contains("content-ignore")) ignoreContentFailure(id);
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
  $("report-date").value = todayInShanghai();
  $("cleanup-before").value = daysAgoInShanghai(90);
  syncCleanupFields();
  refreshIcons();
  try {
    await Promise.all([
      loadCatalogs(), loadStatus(), loadArticles(), loadRuns(), loadSettings(),
      loadAIStatus(), loadAISettings(), loadReports(),
    ]);
  }
  catch (error) { showToast(error.message, true); }
  window.setInterval(pollLiveState, 2000);
}

window.addEventListener("DOMContentLoaded", initialize);
