"""Command-line entry point."""

from __future__ import annotations

import logging

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
def inspect(
    package: str = typer.Option("com.hhaexchange.caregiver", "--package", "-p"),
    text_for: str = typer.Option(
        "", "--text-for",
        help="Comma-separated resource-id suffixes to reveal text for. "
             "Ids that look identifying are refused regardless.",
    ),
    server: str = typer.Option(DEFAULT_SERVER, "--server"),
) -> None:
    """Summarise the current screen without leaking patient data.

    Text is withheld unless asked for by resource-id, and refused outright for
    ids that look identifying. Safe to paste into a chat or an issue.
    """
    from appium import webdriver
    from appium.options.android import UiAutomator2Options

    from apt_log.inspect import inspect_driver

    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.automation_name = "UiAutomator2"
    options.app_package = package
    options.no_reset = True
    options.set_capability("appium:skipDeviceInitialization", True)

    driver = webdriver.Remote(server, options=options)
    try:
        wanted = tuple(x.strip() for x in text_for.split(",") if x.strip())
        typer.echo(inspect_driver(driver, text_for=wanted).render())
    finally:
        driver.quit()


@app.command()
def feed(
    interval: float = typer.Option(1.0, "--interval", "-i",
                                   help="Seconds between frames."),
    serial: str = typer.Option("", "--serial", help="adb serial, if more than one."),
) -> None:
    """Keep the UI's phone view and status panel fed.

    Runs outside Appium so it never competes for the session the agent needs.
    Refuses to capture anything that looks like a credential screen — see
    apt_log.feed for exactly what that means and where it is weak.
    """
    from apt_log.feed import run as run_feed
    from apt_log.ui.state import SCREENSHOT_PATH

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    run_feed(SCREENSHOT_PATH, interval=interval, serial=serial or None)


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
