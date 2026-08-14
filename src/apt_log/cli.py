"""Command-line entry point."""

from __future__ import annotations

from pathlib import Path

import typer

from apt_log import __version__
from apt_log.probe import DEFAULT_SERVER, run_probe
from apt_log.secrets import SECRETS_PATH, FileSecretProvider

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


@app.command("login-check")
def login_check(
    package: str = typer.Option("com.hhaexchange.caregiver", "--package", "-p"),
    activity: str | None = typer.Option(
        None, "--activity", "-a", help="Launch activity, if the default is wrong."
    ),
    server: str = typer.Option(DEFAULT_SERVER, "--server", help="Appium server URL."),
    secrets_file: Path = typer.Option(
        SECRETS_PATH, "--secrets",
        help="File-backed credential store. A password is never accepted as an "
             "argument (REQ-3).",
    ),
    language: str | None = typer.Option(
        None, "--language",
        help="Move the language wheel to this code (en, es, fr, zh, ru, ht, ko) "
             "before applying it. Without this, whatever is highlighted is accepted.",
    ),
    cold_start: bool = typer.Option(
        True, "--cold-start/--no-cold-start",
        help="Force-stop and relaunch first, as a scheduled run does (REQ-2).",
    ),
    clear_data: bool = typer.Option(
        False, "--clear-data",
        help="`pm clear` before launching: wipes the app's local data, which signs "
             "it out and puts the login form back. Use this to test the login path "
             "itself — a force-stop alone leaves the session intact. Implies a "
             "restart, since clearing data kills the process.",
    ),
    timeout: float = typer.Option(
        30.0, "--timeout", help="Seconds to wait for a screen after submitting."
    ),
) -> None:
    """Open the app and sign in, reporting each step. Signs in and stops.

    No agency is selected and nothing is checked off, so this writes nothing to
    the agency's record. Exits non-zero unless a signed-in screen was reached.
    """
    from apt_log.login_check import run_login_check

    report = run_login_check(
        package,
        FileSecretProvider(secrets_file),
        activity=activity,
        server_url=server,
        language=language,
        cold_start=cold_start,
        clear_data=clear_data,
        settle_timeout=timeout,
    )
    typer.echo(report.render())
    raise typer.Exit(code=0 if report.signed_in else 1)


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
