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
    assert 'api("/api/ai/pending/clear"' in javascript
    assert 'api("/api/keyword-categories")' in javascript
    assert "category_id:" in javascript
