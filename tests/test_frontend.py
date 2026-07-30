from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ai_runtime_poll_refreshes_failures_without_manual_reload() -> None:
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "async function pollLiveState()" in javascript
    assert 'state.view === "intelligence" && state.wasAnalysisRunning' in javascript
    assert "loadContentFailures()" in javascript
    assert "loadAIRuns()" in javascript
    assert "window.setInterval(pollLiveState, 2000)" in javascript


def test_ai_controls_support_collection_selection_and_pause() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="ai-collection-run"' in html
    assert 'id="pause-analysis"' in html
    assert 'collection_run_id: collectionRunId' in javascript
    assert 'api("/api/ai/pause", { method: "POST" })' in javascript
    assert "已请求暂停，将在当前文章处理完成后停止" in javascript


def test_pending_cleanup_and_keyword_category_controls_are_wired() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="clear-pending"' in html
    assert 'id="keyword-category-menu"' in html
    assert 'id="keyword-category"' in html
    assert 'id="keyword-hit-summary"' in html
    assert 'api("/api/ai/pending/clear"' in javascript
    assert 'api("/api/keyword-categories")' in javascript
    assert 'api("/api/keyword-hit-stats")' in javascript
    assert "分类相关 ÷ 已审核" in javascript
    assert "business_relevant_count" in javascript
    assert "category_id:" in javascript


def test_prompt_settings_and_cited_report_content_are_wired() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="ai-relevance-prompt"' in html
    assert 'id="ai-report-prompt"' in html
    assert 'id="ai-report-prompt-trade-policy"' not in html
    assert 'id="ai-report-prompt-tariff-adjustment"' not in html
    assert 'id="ai-report-prompt-industry-regulation"' not in html
    assert "相关性审核提示词" in html
    assert 'relevance_prompt: $("ai-relevance-prompt").value.trim()' in javascript
    assert 'report_prompt: $("ai-report-prompt").value.trim()' in javascript
    assert "category_report_prompts:" not in javascript
    assert "function reportSources(" in javascript
    assert "function simplifiedReportSourceTitle(" in javascript
    assert "title.slice(0, limit)" in javascript
    assert '${escapeHtml(linkText)}</a>' in javascript
    assert 'title="${escapeHtml(linkTitle)}"' in javascript
    assert "全部来源文章" in javascript
    assert "item.secondary_categories" in javascript
    assert "按新闻发布日期生成综合日报" in html
    assert 'id="report-category"' not in html
    assert "fillReportKeywordCategoryOptions" not in javascript
    assert "keyword_category_id" not in javascript
    assert "report.keyword_category_name" not in javascript
    assert "article.source_url" in javascript
    assert "关键进展</h4>" not in javascript
    assert 'class="development-list report-aspects"' in javascript
    assert '<h4>${escapeHtml(item.title' in javascript


def test_reports_can_be_manually_sent_to_feishu() -> None:
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "report-feishu-button" in javascript
    assert 'api(`/api/reports/${id}/feishu`, { method: "POST" })' in javascript


def test_incremental_collection_setting_is_wired() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="incremental-collection"' in html
    assert "从上次成功采集时间继续" in html
    assert 'incremental_collection: $("incremental-collection").checked' in javascript
    assert 'id="search-local-keyword-filter"' in html
    assert "直连源仍会复核" in html
    assert 'search_local_keyword_filter: $("search-local-keyword-filter").checked' in javascript

def test_source_site_domain_setting_is_wired() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="source-site-domain"' in html
    assert "站点限制（搜索型可选，可填写多个）" in html
    assert "reuters.com OR cls.cn" in html
    assert 'site_domain: $("source-site-domain").value.trim()' in javascript
    assert "syncSourceSiteField" in javascript


def test_web_crawler_source_mode_is_wired() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert '<option value="crawler">网页爬虫（无 RSS）</option>' in html
    assert 'id="source-url-hint"' in html
    assert "网页爬虫兜底由系统设置控制" in html
    assert "网页爬虫兜底默认关闭" in html
    assert "开启总开关后，RSS 不可用时才会切换" in html
    assert "采集 / 兜底策略" in html
    assert 'crawler: "网页爬虫"' in javascript
    assert 'mode === "crawler" ? "新闻列表页地址"' in javascript
    assert "function sourceFallbackMarkup(mode)" in javascript
    assert "RSS 优先 · 自动兜底" in javascript
    assert "直接网页爬虫" in javascript
    assert "反爬状态" in html
    assert "遵守 robots.txt" in html
    assert 'id="crawler-enabled" type="checkbox"' in html
    assert 'id="crawler-respect-robots"' in html
    assert 'id="crawler-min-interval"' in html
    assert 'id="crawler-cooldown-minutes"' in html
    assert "function sourceCrawlerHealthMarkup(item)" in javascript
    assert "crawler_in_cooldown" in javascript
    assert 'crawler_enabled: $("crawler-enabled").checked' in javascript
    assert "function syncCrawlerSettingsAvailability()" in javascript
    assert 'crawler_respect_robots: $("crawler-respect-robots").checked' in javascript
