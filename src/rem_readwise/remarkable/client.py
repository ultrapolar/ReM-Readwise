"""reMarkable Cloud access by driving the ``rmapi`` CLI as a subprocess.

We shell out to ddvk/rmapi (https://github.com/ddvk/rmapi) rather than
re-implementing the cloud sync protocol in Python. rmapi tracks the current
"sync 1.5" protocol and handles the device pairing / token refresh dance, so
this stays robust as reMarkable evolves the cloud API.

The auth token lives in the file pointed to by ``RMAPI_CONFIG`` — keep that on
a persistent volume so the container does not need re-pairing on restart.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class RemarkableError(RuntimeError):
    """Raised when an rmapi invocation fails."""


@dataclass(frozen=True)
class RemarkableEntry:
    """An item listed in a reMarkable cloud folder."""

    name: str
    is_dir: bool


class RemarkableClient:
    """Minimal reMarkable Cloud client backed by the rmapi binary."""

    def __init__(self, rmapi_path: str = "rmapi", config_path: str | None = None) -> None:
        self._rmapi = rmapi_path
        self._config_path = config_path

    # ── low-level invocation ──────────────────────────────────────────────
    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._config_path:
            env["RMAPI_CONFIG"] = self._config_path
        return env

    def _run(
        self,
        args: list[str],
        *,
        stdin: str | None = None,
        check: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self._rmapi, *args]
        logger.debug("rmapi %s", " ".join(args))
        proc = subprocess.run(  # noqa: S603 - args are constructed internally
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            env=self._env(),
            cwd=str(cwd) if cwd else None,
        )
        if check and proc.returncode != 0:
            raise RemarkableError(
                f"rmapi {' '.join(args)} failed (exit {proc.returncode}):\n"
                f"{proc.stdout}\n{proc.stderr}".strip()
            )
        return proc

    # ── auth ──────────────────────────────────────────────────────────────
    def is_authenticated(self) -> bool:
        """True if rmapi already holds a valid cloud token."""
        proc = self._run(["account"], check=False)
        if proc.returncode == 0:
            return True
        # Older rmapi builds lack `account`; fall back to a cheap listing.
        return self._run(["ls"], check=False).returncode == 0

    def register(self, one_time_code: str) -> None:
        """Pair with the reMarkable Cloud using a one-time code.

        Get the 8-character code from https://my.remarkable.com/device/desktop/connect
        (or .../device/browser/connect). rmapi reads it from stdin on first use
        and writes a long-lived token to RMAPI_CONFIG.
        """
        code = one_time_code.strip()
        if not code:
            raise RemarkableError("A one-time pairing code is required.")
        if self._config_path:
            Path(self._config_path).parent.mkdir(parents=True, exist_ok=True)
        proc = self._run(["ls"], stdin=f"{code}\n", check=False)
        if not self.is_authenticated():
            raise RemarkableError(
                "Pairing failed. Double-check the one-time code (they expire quickly).\n"
                f"{proc.stdout}\n{proc.stderr}".strip()
            )
        logger.info("reMarkable cloud pairing succeeded.")

    # ── folders / listing ─────────────────────────────────────────────────
    def ensure_folder(self, path: str) -> None:
        """Create ``path`` (e.g. ``Readwise``) if it does not already exist."""
        proc = self._run(["mkdir", _abs(path)], check=False)
        out = f"{proc.stdout}\n{proc.stderr}".lower()
        if proc.returncode != 0 and "exist" not in out:
            raise RemarkableError(f"Could not create folder {path!r}:\n{out.strip()}")

    def list_folder(self, path: str) -> list[RemarkableEntry]:
        """List the entries directly under ``path``."""
        proc = self._run(["ls", _abs(path)], check=False)
        if proc.returncode != 0:
            return []
        return _parse_ls(proc.stdout)

    def stat(self, remote_path: str) -> str | None:
        """Return the stable cloud document ID for ``remote_path``, or None.

        Document IDs survive on-device renames, which is what makes the sync
        mapping rename-proof. Best-effort by design: rmapi's ``stat`` output
        varies by version (Go struct dump vs JSON), and a doc that can't be
        stat'ed simply falls back to name-based matching.
        """
        proc = self._run(["stat", _abs(remote_path)], check=False)
        if proc.returncode != 0:
            logger.debug("rmapi stat %r failed:\n%s", remote_path, proc.stderr.strip())
            return None
        return _parse_stat_id(proc.stdout)

    # ── transfers ─────────────────────────────────────────────────────────
    def upload_pdf(self, local_pdf: Path, dest_folder: str) -> None:
        """Upload a PDF into ``dest_folder``. The document name is the filename."""
        self.ensure_folder(dest_folder)
        self._run(["put", str(local_pdf), _abs(dest_folder)])
        logger.info("Uploaded %s to reMarkable:%s", local_pdf.name, dest_folder)

    def download(self, remote_path: str, dest_dir: Path) -> Path:
        """Download a document (with its annotation .rm files) as a zip archive.

        Returns the path to the downloaded archive. rmapi writes a ``.rmdoc``
        (zip) named after the document into the working directory.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        before = set(dest_dir.iterdir())
        self._run(["get", _abs(remote_path)], cwd=dest_dir)
        produced = sorted(
            p for p in dest_dir.iterdir()
            if p not in before and p.suffix in {".zip", ".rmdoc", ".rmn"}
        )
        if not produced:
            raise RemarkableError(
                f"rmapi get {remote_path!r} produced no archive in {dest_dir}."
            )
        return produced[-1]


def _abs(path: str) -> str:
    """rmapi treats paths as absolute from the cloud root when prefixed with /."""
    path = path.strip()
    if not path or path == "/":
        return "/"
    return path if path.startswith("/") else f"/{path}"


_STAT_ID_RE = re.compile(r"\bI[Dd]:\s*([0-9a-fA-F][0-9a-fA-F-]{7,})")


def _parse_stat_id(output: str) -> str | None:
    """Extract the document ID from ``rmapi stat`` output.

    Newer rmapi builds print JSON; older ones print Go's ``%+v`` struct dump
    (``&{ID:uuid Version:2 ...}``). Try JSON first, then the regex.
    """
    text = output.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
            if isinstance(payload, list):
                payload = payload[0] if payload else {}
            for key in ("ID", "Id", "id"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
        except (json.JSONDecodeError, AttributeError, IndexError):
            pass
    match = _STAT_ID_RE.search(text)
    return match.group(1) if match else None


def _parse_ls(output: str) -> list[RemarkableEntry]:
    """Parse ``rmapi ls`` output.

    rmapi prints one entry per line, prefixing directories with ``[d]`` and
    files with ``[f]``. Be lenient about exact formatting across versions.
    """
    entries: list[RemarkableEntry] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        is_dir = False
        name = line
        if line.startswith("[d]"):
            is_dir, name = True, line[3:].strip()
        elif line.startswith("[f]"):
            is_dir, name = False, line[3:].strip()
        elif line.endswith("/"):
            is_dir, name = True, line.rstrip("/").strip()
        if name and name not in {".", ".."}:
            entries.append(RemarkableEntry(name=name, is_dir=is_dir))
    return entries
