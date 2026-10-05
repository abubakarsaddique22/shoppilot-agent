import json

from fastapi.testclient import TestClient

from shoppilot.core.logging import bind_context, get_logger, mask, setup_logging


def test_mask():
    assert "ali.raza@gmail.com" not in mask("mail ali.raza@gmail.com")
    assert "SECRET" not in mask("api_key=SECRET")
    assert "abc" not in mask("Authorization: Bearer abc.def")


def test_files_written(tmp_path):
    setup_logging("INFO", tmp_path, console=False)
    log = get_logger("t")
    with bind_context(ticket_id="T-1"):
        log.info("hello")
        try:
            _ = 1 / 0
        except ZeroDivisionError:
            log.exception("boom")
    first = [json.loads(x) for x in (tmp_path / "app.log").read_text().splitlines()]
    assert any(e["ticket_id"] == "T-1" and e["msg"] == "hello" for e in first)
    assert "ZeroDivisionError" in (tmp_path / "error.log").read_text()


def test_api_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from shoppilot.api.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/v1/debug/not-found")
        assert r.status_code == 404 and r.json()["error"]["code"] == "ORDER_NOT_FOUND"
        r = c.get("/v1/debug/boom")
        assert r.status_code == 500 and "Traceback" not in r.text
