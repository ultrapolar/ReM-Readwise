"""Heartbeat: status-file writing and webhook alert/recovery thresholds."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
import respx

from rem_readwise.config import Settings
from rem_readwise.heartbeat import Heartbeat
from rem_readwise.sync.engine import CycleResult, SyncEngine
from rem_readwise.sync.forward import ForwardResult
from rem_readwise.sync.inbox import InboxResult
from rem_readwise.sync.reverse import ReverseResult

WEBHOOK = "https://discord.example/api/webhooks/1/tok"


def make_cycle() -> CycleResult:
    return CycleResult(
        forward=ForwardResult(considered=4, uploaded=2, failed=1),
        inbox=InboxResult(considered=1, uploaded=1),
        reverse=ReverseResult(
            documents_scanned=3, highlights_pushed=5, documents_unmatched=1
        ),
    )


def read_status(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))


# ── status file ─────────────────────────────────────────────────────────────


def test_success_writes_status_with_cycle_counts(tmp_path):
    status = tmp_path / "status.json"
    Heartbeat(status).record_success(make_cycle())

    data = read_status(status)
    assert data["ok"] is True
    assert data["consecutive_failures"] == 0
    assert data["last_error"] is None
    assert data["uploaded"] == 3  # forward + inbox
    assert data["highlights_pushed"] == 5
    assert data["unmatched"] == 1
    assert data["failed"] == 1
    # ISO-8601 UTC timestamp, written atomically (no stray tmp file left).
    stamp = dt.datetime.fromisoformat(data["timestamp"])
    assert stamp.utcoffset() == dt.timedelta(0)
    assert not (tmp_path / "status.json.tmp").exists()


def test_failure_then_success_resets_counters(tmp_path):
    status = tmp_path / "status.json"
    hb = Heartbeat(status)

    hb.record_failure(RuntimeError("boom"))
    data = read_status(status)
    assert data["ok"] is False
    assert data["consecutive_failures"] == 1
    assert data["last_error"] == "RuntimeError: boom"
    assert "uploaded" not in data  # counts only appear on success

    hb.record_failure("second cycle exploded")
    data = read_status(status)
    assert data["consecutive_failures"] == 2
    assert data["last_error"] == "second cycle exploded"

    hb.record_success(make_cycle())
    data = read_status(status)
    assert data["ok"] is True
    assert data["consecutive_failures"] == 0
    assert data["last_error"] is None


def test_unwritable_status_path_never_raises(tmp_path):
    target = tmp_path / "status.json"
    target.mkdir()  # a directory where the file should go: the rename fails
    hb = Heartbeat(target)
    hb.record_failure(RuntimeError("boom"))  # must not raise
    hb.record_success(make_cycle())  # must not raise


# ── webhook alerting ────────────────────────────────────────────────────────


@respx.mock
def test_alert_fires_once_at_threshold(tmp_path):
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
    hb = Heartbeat(tmp_path / "status.json", webhook_url=WEBHOOK, alert_after=3)

    hb.record_failure(RuntimeError("boom 1"))
    hb.record_failure(RuntimeError("boom 2"))
    assert route.call_count == 0  # below the threshold

    hb.record_failure(RuntimeError("boom 3"))
    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert "3 consecutive" in body["content"]
    assert "boom 3" in body["content"]

    hb.record_failure(RuntimeError("boom 4"))
    hb.record_failure(RuntimeError("boom 5"))
    assert route.call_count == 1  # once per outage, not per failing cycle


@respx.mock
def test_recovery_message_then_new_outage_alerts_again(tmp_path):
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
    hb = Heartbeat(tmp_path / "status.json", webhook_url=WEBHOOK, alert_after=2)

    hb.record_failure(RuntimeError("a"))
    hb.record_failure(RuntimeError("b"))
    assert route.call_count == 1  # the alert

    hb.record_success(make_cycle())
    assert route.call_count == 2
    body = json.loads(route.calls.last.request.content)
    assert "RECOVERED" in body["content"]
    assert "2 failed cycle(s)" in body["content"]

    hb.record_success(make_cycle())
    assert route.call_count == 2  # recovery announced once

    hb.record_failure(RuntimeError("c"))
    hb.record_failure(RuntimeError("d"))
    assert route.call_count == 3  # a fresh outage alerts again


@respx.mock
def test_no_recovery_message_without_prior_alert(tmp_path):
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
    hb = Heartbeat(tmp_path / "status.json", webhook_url=WEBHOOK, alert_after=3)

    hb.record_failure(RuntimeError("blip"))
    hb.record_success(make_cycle())
    assert route.call_count == 0


@respx.mock
def test_webhook_errors_never_crash_and_alert_is_retried(tmp_path):
    route = respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError("down"))
    hb = Heartbeat(tmp_path / "status.json", webhook_url=WEBHOOK, alert_after=2)

    hb.record_failure(RuntimeError("a"))
    hb.record_failure(RuntimeError("b"))  # alert attempt fails; must not raise
    assert route.call_count == 1
    assert read_status(tmp_path / "status.json")["consecutive_failures"] == 2

    route.mock(return_value=httpx.Response(204))
    hb.record_failure(RuntimeError("c"))  # the undelivered alert is retried
    assert route.call_count == 2

    hb.record_failure(RuntimeError("d"))  # ...and once delivered, no more spam
    assert route.call_count == 2


def test_no_webhook_configured_never_posts(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "rem_readwise.heartbeat.httpx.post",
        lambda url, **kw: calls.append(url),
    )
    hb = Heartbeat(tmp_path / "status.json", webhook_url=None, alert_after=1)
    hb.record_failure(RuntimeError("boom"))
    hb.record_failure(RuntimeError("boom again"))
    hb.record_success(make_cycle())
    assert calls == []


# ── engine wiring ───────────────────────────────────────────────────────────


def _engine_settings(tmp_path) -> Settings:
    return Settings(
        readwise_token="tok",
        state_path=str(tmp_path / "state.json"),
        status_path=str(tmp_path / "status.json"),
        work_dir=str(tmp_path / "work"),
        inbox_dir=str(tmp_path / "inbox"),
        _env_file=None,
    )


def _interrupt_sleep(_seconds) -> None:
    raise KeyboardInterrupt  # break out of run_forever after one cycle


def test_run_forever_records_failure_in_status(tmp_path, monkeypatch):
    engine = SyncEngine(_engine_settings(tmp_path))

    def wedged():
        raise RuntimeError("wedged")

    monkeypatch.setattr(engine, "run_once", wedged)
    monkeypatch.setattr("rem_readwise.sync.engine.time.sleep", _interrupt_sleep)
    with pytest.raises(KeyboardInterrupt):
        engine.run_forever()

    data = read_status(tmp_path / "status.json")
    assert data["ok"] is False
    assert data["consecutive_failures"] == 1
    assert "wedged" in data["last_error"]


def test_run_forever_records_success_in_status(tmp_path, monkeypatch):
    engine = SyncEngine(_engine_settings(tmp_path))
    monkeypatch.setattr(engine, "run_once", make_cycle)
    monkeypatch.setattr("rem_readwise.sync.engine.time.sleep", _interrupt_sleep)
    with pytest.raises(KeyboardInterrupt):
        engine.run_forever()

    data = read_status(tmp_path / "status.json")
    assert data["ok"] is True
    assert data["uploaded"] == 3
    assert data["highlights_pushed"] == 5
