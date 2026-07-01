import subprocess
from pathlib import Path

import pytest

from rem_readwise.remarkable import client as rm_client
from rem_readwise.remarkable.client import (
    RemarkableClient,
    RemarkableError,
    _abs,
    _parse_ls,
)


class ScriptedRmapi:
    """Stands in for subprocess.run, dispatching on the rmapi verb.

    ``responses`` maps a verb (``ls``, ``mkdir``, ...) to either a
    ``(returncode, stdout, stderr)`` tuple or a callable ``(args, cwd) ->``
    that tuple. Unknown verbs succeed with empty output. Every invocation is
    recorded in ``calls``.
    """

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls: list[dict] = []

    def __call__(self, cmd, *, input=None, capture_output=False, text=False, env=None, cwd=None):
        args = list(cmd[1:])
        self.calls.append({"args": args, "stdin": input, "env": env, "cwd": cwd})
        response = self.responses.get(args[0] if args else "", (0, "", ""))
        if callable(response):
            response = response(args, cwd)
        returncode, stdout, stderr = response
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    def verbs(self) -> list[str]:
        return [call["args"][0] for call in self.calls]


@pytest.fixture
def rmapi(monkeypatch):
    def install(responses=None):
        scripted = ScriptedRmapi(responses)
        monkeypatch.setattr(rm_client.subprocess, "run", scripted)
        return scripted

    return install


# ── invocation / env ────────────────────────────────────────────────────────
def test_config_path_is_passed_via_env(rmapi):
    scripted = rmapi()
    RemarkableClient(config_path="/data/rmapi.conf").is_authenticated()
    assert scripted.calls[0]["env"]["RMAPI_CONFIG"] == "/data/rmapi.conf"


def test_failed_command_raises_with_output(rmapi):
    scripted = rmapi({"put": (1, "some stdout", "some stderr")})
    client = RemarkableClient()
    with pytest.raises(RemarkableError, match=r"exit 1") as excinfo:
        client.upload_pdf(Path("/tmp/x.pdf"), "Readwise")
    assert "some stderr" in str(excinfo.value)
    assert scripted.verbs() == ["mkdir", "put"]


# ── auth ────────────────────────────────────────────────────────────────────
def test_is_authenticated_when_account_succeeds(rmapi):
    scripted = rmapi({"account": (0, "user@example.com", "")})
    assert RemarkableClient().is_authenticated() is True
    assert scripted.verbs() == ["account"]


def test_is_authenticated_falls_back_to_ls_for_old_rmapi(rmapi):
    scripted = rmapi({"account": (1, "", "unknown command"), "ls": (0, "[d] Books", "")})
    assert RemarkableClient().is_authenticated() is True
    assert scripted.verbs() == ["account", "ls"]


def test_is_authenticated_false_when_both_probes_fail(rmapi):
    rmapi({"account": (1, "", ""), "ls": (1, "", "not registered")})
    assert RemarkableClient().is_authenticated() is False


def test_register_rejects_empty_code(rmapi):
    scripted = rmapi()
    with pytest.raises(RemarkableError, match="one-time pairing code"):
        RemarkableClient().register("   ")
    assert scripted.calls == []


def test_register_feeds_code_on_stdin_and_creates_config_dir(rmapi, tmp_path):
    scripted = rmapi({"ls": (0, "", ""), "account": (0, "", "")})
    config = tmp_path / "deep" / "nested" / "rmapi.conf"
    RemarkableClient(config_path=str(config)).register("  abcd1234\n")
    assert config.parent.is_dir()
    first = scripted.calls[0]
    assert first["args"] == ["ls"]
    assert first["stdin"] == "abcd1234\n"


def test_register_raises_when_pairing_fails(rmapi):
    rmapi({"ls": (1, "", "invalid code"), "account": (1, "", "")})
    with pytest.raises(RemarkableError, match="Pairing failed"):
        RemarkableClient().register("abcd1234")


# ── folders / listing ───────────────────────────────────────────────────────
def test_ensure_folder_tolerates_already_existing(rmapi):
    rmapi({"mkdir": (1, "", "directory already exists")})
    RemarkableClient().ensure_folder("Readwise")  # must not raise


def test_ensure_folder_raises_on_other_failures(rmapi):
    rmapi({"mkdir": (1, "", "connection refused")})
    with pytest.raises(RemarkableError, match="Could not create folder"):
        RemarkableClient().ensure_folder("Readwise")


def test_list_folder_uses_absolute_path_and_parses(rmapi):
    scripted = rmapi({"ls": (0, "[d] Books\n[f] My Paper\n", "")})
    entries = RemarkableClient().list_folder("Readwise")
    assert scripted.calls[0]["args"] == ["ls", "/Readwise"]
    assert [(e.name, e.is_dir) for e in entries] == [("Books", True), ("My Paper", False)]


def test_list_folder_returns_empty_on_error(rmapi):
    rmapi({"ls": (1, "", "no such folder")})
    assert RemarkableClient().list_folder("Nope") == []


# ── transfers ───────────────────────────────────────────────────────────────
def test_upload_pdf_ensures_folder_then_puts(rmapi, tmp_path):
    scripted = rmapi()
    pdf = tmp_path / "Doc.pdf"
    pdf.write_bytes(b"%PDF-")
    RemarkableClient().upload_pdf(pdf, "Readwise")
    assert scripted.verbs() == ["mkdir", "put"]
    assert scripted.calls[1]["args"] == ["put", str(pdf), "/Readwise"]


def test_download_returns_only_the_new_archive(rmapi, tmp_path):
    dest = tmp_path / "downloads"
    dest.mkdir()
    (dest / "old.rmdoc").write_bytes(b"stale")

    def fake_get(args, cwd):
        (Path(cwd) / "Doc.rmdoc").write_bytes(b"fresh")
        return (0, "", "")

    scripted = rmapi({"get": fake_get})
    archive = RemarkableClient().download("Readwise/Doc", dest)
    assert archive == dest / "Doc.rmdoc"
    assert scripted.calls[0]["cwd"] == str(dest)
    assert scripted.calls[0]["args"] == ["get", "/Readwise/Doc"]


def test_download_raises_when_no_archive_produced(rmapi, tmp_path):
    rmapi()  # `get` succeeds but writes nothing
    with pytest.raises(RemarkableError, match="produced no archive"):
        RemarkableClient().download("Readwise/Doc", tmp_path / "downloads")


def test_abs_normalizes_paths():
    assert _abs("Readwise") == "/Readwise"
    assert _abs("/Readwise") == "/Readwise"
    assert _abs("  Readwise/Sub ") == "/Readwise/Sub"
    assert _abs("") == "/"
    assert _abs("/") == "/"


def test_parse_ls_handles_prefixed_entries():
    out = "[d] Books\n[f] My Paper\n[f] Another Doc\n"
    entries = _parse_ls(out)
    assert [(e.name, e.is_dir) for e in entries] == [
        ("Books", True),
        ("My Paper", False),
        ("Another Doc", False),
    ]


def test_parse_ls_handles_trailing_slash_dirs_and_blanks():
    out = "Notebooks/\nplain file\n\n"
    entries = _parse_ls(out)
    assert ("Notebooks", True) in [(e.name, e.is_dir) for e in entries]
    assert ("plain file", False) in [(e.name, e.is_dir) for e in entries]
