"""CLI command tests, driven through Typer's CliRunner (no network/device)."""

from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from rem_readwise import cli
from rem_readwise.config import Settings
from rem_readwise.sync.engine import CycleResult
from rem_readwise.sync.forward import ForwardResult
from rem_readwise.sync.inbox import InboxResult
from rem_readwise.sync.reverse import ReverseResult

runner = CliRunner()


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "readwise_token": "test-token",
        "state_path": str(tmp_path / "state.json"),
        "work_dir": str(tmp_path / "work"),
        "inbox_dir": str(tmp_path / "inbox"),
        "rmapi_config": str(tmp_path / "rmapi.conf"),
        **overrides,
    }
    return Settings(_env_file=None, **values)


class FakeRemarkableClient:
    """Stands in for cli.RemarkableClient; behavior set via class attributes."""

    authenticated = True
    registered_codes: list[str] = []

    def __init__(self, rmapi_path=None, config_path=None):
        pass

    def is_authenticated(self):
        return self.authenticated

    def register(self, code):
        type(self).registered_codes.append(code)


class FakeEngine:
    """Stands in for cli.SyncEngine; records the settings it was built with."""

    last_settings: Settings | None = None
    result = CycleResult(
        forward=ForwardResult(considered=4, uploaded=2, skipped_existing=1, failed=1),
        inbox=InboxResult(),
        reverse=ReverseResult(documents_scanned=3, highlights_pushed=5),
    )

    def __init__(self, settings):
        type(self).last_settings = settings
        self.state = SimpleNamespace(uploaded_count=7, pushed_count=12)

    def run_once(self):
        return self.result


def use_settings(monkeypatch, settings):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)


# ── auth-remarkable ─────────────────────────────────────────────────────────
def test_auth_remarkable_registers_the_code(tmp_path, monkeypatch):
    use_settings(monkeypatch, make_settings(tmp_path))
    monkeypatch.setattr(cli, "RemarkableClient", FakeRemarkableClient)
    monkeypatch.setattr(FakeRemarkableClient, "registered_codes", [])

    result = runner.invoke(cli.app, ["auth-remarkable", "abcd1234"])

    assert result.exit_code == 0
    assert FakeRemarkableClient.registered_codes == ["abcd1234"]
    assert "paired" in result.output


# ── auth-check ──────────────────────────────────────────────────────────────
def test_auth_check_passes_when_both_sides_are_ready(tmp_path, monkeypatch):
    use_settings(monkeypatch, make_settings(tmp_path))
    monkeypatch.setattr(cli, "RemarkableClient", FakeRemarkableClient)
    monkeypatch.setattr(FakeRemarkableClient, "authenticated", True)

    result = runner.invoke(cli.app, ["auth-check"])

    assert result.exit_code == 0
    assert "✓ Readwise token is set" in result.output
    assert "✓ reMarkable is paired" in result.output


def test_auth_check_fails_without_readwise_token(tmp_path, monkeypatch):
    use_settings(monkeypatch, make_settings(tmp_path, readwise_token=""))
    monkeypatch.setattr(cli, "RemarkableClient", FakeRemarkableClient)
    monkeypatch.setattr(FakeRemarkableClient, "authenticated", True)

    result = runner.invoke(cli.app, ["auth-check"])

    assert result.exit_code == 1
    assert "✗ READWISE_TOKEN is not set" in result.output


def test_auth_check_fails_when_remarkable_unpaired(tmp_path, monkeypatch):
    use_settings(monkeypatch, make_settings(tmp_path))
    monkeypatch.setattr(cli, "RemarkableClient", FakeRemarkableClient)
    monkeypatch.setattr(FakeRemarkableClient, "authenticated", False)

    result = runner.invoke(cli.app, ["auth-check"])

    assert result.exit_code == 1
    assert "✗ reMarkable is not paired" in result.output


# ── sync ────────────────────────────────────────────────────────────────────
def test_sync_reports_cycle_counts(tmp_path, monkeypatch):
    use_settings(monkeypatch, make_settings(tmp_path))
    monkeypatch.setattr(cli, "SyncEngine", FakeEngine)

    result = runner.invoke(cli.app, ["sync"])

    assert result.exit_code == 0
    assert "2 uploaded" in result.output
    assert "1 already present" in result.output
    assert "5 highlight(s) pushed" in result.output
    assert FakeEngine.last_settings.dry_run is False


def test_sync_dry_run_flag_overrides_settings(tmp_path, monkeypatch):
    use_settings(monkeypatch, make_settings(tmp_path, dry_run=False))
    monkeypatch.setattr(cli, "SyncEngine", FakeEngine)

    result = runner.invoke(cli.app, ["sync", "--dry-run"])

    assert result.exit_code == 0
    assert FakeEngine.last_settings.dry_run is True


# ── run ─────────────────────────────────────────────────────────────────────
def test_run_starts_the_forever_loop(tmp_path, monkeypatch):
    use_settings(monkeypatch, make_settings(tmp_path))
    monkeypatch.setattr(cli, "SyncEngine", FakeEngine)
    started = []
    monkeypatch.setattr(
        FakeEngine, "run_forever", lambda self: started.append(True), raising=False
    )

    result = runner.invoke(cli.app, ["run"])

    assert result.exit_code == 0
    assert started == [True]


# ── status ──────────────────────────────────────────────────────────────────
def test_status_reports_state_counters(tmp_path, monkeypatch):
    use_settings(monkeypatch, make_settings(tmp_path))
    monkeypatch.setattr(cli, "SyncEngine", FakeEngine)

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0
    assert "Uploaded documents: 7" in result.output
    assert "Highlights pushed:  12" in result.output
