from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ai_runtime_poll_refreshes_failures_without_manual_reload() -> None:
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "async function pollLiveState()" in javascript
    assert 'state.view === "intelligence" && state.wasAnalysisRunning' in javascript
    assert "loadContentFailures()" in javascript
    assert "loadAIRuns()" in javascript
    assert "window.setInterval(pollLiveState, 2000)" in javascript
