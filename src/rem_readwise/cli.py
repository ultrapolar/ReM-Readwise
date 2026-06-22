"""Command-line interface for rem-readwise."""

from __future__ import annotations

import logging

import typer

from rem_readwise.config import load_settings
from rem_readwise.logging_config import configure_logging
from rem_readwise.remarkable import RemarkableClient
from rem_readwise.sync.engine import SyncEngine

app = typer.Typer(
    add_completion=False,
    help="Two-way sync between Readwise Reader PDFs and a reMarkable tablet.",
)
logger = logging.getLogger(__name__)


@app.command("auth-remarkable")
def auth_remarkable(
    code: str = typer.Argument(
        ...,
        help="One-time code from https://my.remarkable.com/device/desktop/connect",
    ),
) -> None:
    """Pair this tool with your reMarkable Cloud account."""
    settings = load_settings()
    configure_logging(settings.log_level)
    client = RemarkableClient(
        rmapi_path=settings.rmapi_path, config_path=settings.rmapi_config
    )
    client.register(code)
    typer.secho("✓ reMarkable paired. Token stored at "
                f"{settings.rmapi_config}", fg=typer.colors.GREEN)


@app.command("auth-check")
def auth_check() -> None:
    """Verify Readwise token and reMarkable pairing are both working."""
    settings = load_settings()
    configure_logging(settings.log_level)

    ok = True
    if settings.readwise_token:
        typer.secho("✓ Readwise token is set", fg=typer.colors.GREEN)
    else:
        typer.secho("✗ READWISE_TOKEN is not set", fg=typer.colors.RED)
        ok = False

    client = RemarkableClient(
        rmapi_path=settings.rmapi_path, config_path=settings.rmapi_config
    )
    if client.is_authenticated():
        typer.secho("✓ reMarkable is paired", fg=typer.colors.GREEN)
    else:
        typer.secho("✗ reMarkable is not paired (run auth-remarkable)", fg=typer.colors.RED)
        ok = False

    raise typer.Exit(code=0 if ok else 1)


@app.command("sync")
def sync(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Log actions without uploading or pushing."
    ),
) -> None:
    """Run a single full sync cycle and exit."""
    settings = load_settings()
    configure_logging(settings.log_level)
    if dry_run:
        settings.dry_run = True

    engine = SyncEngine(settings)
    result = engine.run_once()
    typer.secho(
        f"Forward: {result.forward.uploaded} uploaded, "
        f"{result.forward.skipped_existing} already present, "
        f"{result.forward.skipped_no_source} unretrievable, "
        f"{result.forward.failed} failed.",
        fg=typer.colors.CYAN,
    )
    typer.secho(
        f"Inbox:   {result.inbox.uploaded} uploaded, "
        f"{result.inbox.skipped_existing} already present, "
        f"{result.inbox.failed} failed.",
        fg=typer.colors.CYAN,
    )
    typer.secho(
        f"Reverse: {result.reverse.highlights_pushed} highlight(s) pushed "
        f"from {result.reverse.documents_scanned} document(s).",
        fg=typer.colors.CYAN,
    )


@app.command("run")
def run() -> None:
    """Run the sync service forever (the Docker entrypoint)."""
    settings = load_settings()
    configure_logging(settings.log_level)
    SyncEngine(settings).run_forever()


@app.command("status")
def status() -> None:
    """Show how many documents and highlights have been synced so far."""
    settings = load_settings()
    configure_logging(settings.log_level)
    engine = SyncEngine(settings)
    typer.echo(f"Uploaded documents: {engine.state.uploaded_count}")
    typer.echo(f"Highlights pushed:  {engine.state.pushed_count}")


def main() -> None:  # pragma: no cover - thin wrapper
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
