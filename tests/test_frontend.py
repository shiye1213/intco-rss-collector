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
    assert 'id="ai-report-prompt-trade-policy"' in html
    assert 'id="ai-report-prompt-tariff-adjustment"' in html
    assert 'id="ai-report-prompt-industry-regulation"' in html
    assert "相关性审核提示词" in html
    assert 'relevance_prompt: $("ai-relevance-prompt").value.trim()' in javascript
    assert 'report_prompt: $("ai-report-prompt").value.trim()' in javascript
    assert "category_report_prompts:" in javascript
    assert '"贸易政策": $("ai-report-prompt-trade-policy").value.trim()' in javascript
    assert '"关税调整": $("ai-report-prompt-tariff-adjustment").value.trim()' in javascript
    assert '"行业法规": $("ai-report-prompt-industry-regulation").value.trim()' in javascript
    assert "function reportSources(" in javascript
    assert "全部来源文章" in javascript
    assert "item.secondary_categories" in javascript
    assert "按新闻发布日期与关键词分类分别生成" in html
    assert "请选择关键词分类" in html
    assert "fillReportKeywordCategoryOptions()" in javascript
    assert "keyword_category_id: keywordCategoryId" in javascript
    assert "report.keyword_category_name" in javascript
    assert "article.source_url" in javascript


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
    assert "RSS 优先，无 RSS 时使用网页爬虫" in html
    assert "网页爬虫兜底已启用" in html
    assert "RSS 不可用时，采集任务自动切换到同站网页爬虫" in html
    assert "采集 / 兜底策略" in html
    assert 'crawler: "网页爬虫"' in javascript
    assert 'mode === "crawler" ? "新闻列表页地址"' in javascript
    assert "function sourceFallbackMarkup(mode)" in javascript
    assert "RSS 优先 · 自动兜底" in javascript
    assert "直接网页爬虫" in javascript
