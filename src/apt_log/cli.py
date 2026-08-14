"""Command-line entry point."""

from __future__ import annotations

import typer

from apt_log import __version__
from apt_log.probe import DEFAULT_SERVER, run_probe

app = typer.Typer(add_completion=False, help="Automated patient time log controller.")


@app.command()
def version() -> None:
    typer.echo(__version__)


@app.command()
def probe(
    package: str = typer.Option(
        ...,
        "--package",
        "-p",
        help="Target app package, e.g. com.vendor.app. Find it with "
        "`adb shell pm list packages`.",
    ),
    activity: str | None = typer.Option(
        None, "--activity", "-a", help="Launch activity, if the default is wrong."
    ),
    server: str = typer.Option(DEFAULT_SERVER, "--server", help="Appium server URL."),
) -> None:
    """Run the REQ-1 feasibility probe and print a PASS/FAIL report.

    Required before any feature work. Exits non-zero on a blocking failure, so it
    can gate a pipeline as well as a person.
    """
    report = run_probe(package=package, activity=activity, server_url=server)
    typer.echo(report.render())
    raise typer.Exit(code=0 if report.viable else 1)


@app.command()
def run(
    daemon: bool = typer.Option(False, "--daemon", help="Run as a long-lived service."),
) -> None:
    """Run the agent. Not implemented until REQ-1 passes."""
    typer.echo(
        "Not implemented. REQ-1 gates all feature work — run `apt-log probe` first."
    )
    raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
