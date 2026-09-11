"""Install the project as user-scoped macOS launchd jobs."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent.parent
LABEL_PREFIX = "com.aceler.twenty-hermes"
BACKGROUND_BUNDLE_ID = f"{LABEL_PREFIX}.background"
BACKGROUND_APP_NAME = "Twenty Hermes Background.app"
LAUNCH_SERVICES_REGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)
SERVICE_NAMES = ("outbox", "review", "poller")


def background_app_info() -> dict[str, Any]:
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": "twenty-hermes-background",
        "CFBundleIdentifier": BACKGROUND_BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Twenty Hermes Background",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSUIElement": True,
        "NSLocalNetworkUsageDescription": (
            "Twenty Hermes connects to CRM and Outbox databases on the local network."
        ),
    }


def background_app_executable(home: Path | None = None) -> Path:
    return (
        (home or Path.home())
        / "Applications"
        / BACKGROUND_APP_NAME
        / "Contents"
        / "MacOS"
        / "twenty-hermes-background"
    )


def service_payloads(
    project_dir: Path = PROJECT_DIR,
    log_dir: Path | None = None,
    launcher: Path | None = None,
) -> dict[str, dict[str, Any]]:
    log_dir = log_dir or project_dir / "outputs" / "macos-service"
    launcher = launcher or background_app_executable()
    commands = {
        "outbox": [
            str(launcher),
            str(project_dir / "bin" / "start-outbox-service"),
        ],
        "review": [str(launcher), str(project_dir / "bin" / "start-review-ui")],
        "poller": [
            str(launcher),
            "/usr/bin/caffeinate",
            "-is",
            str(project_dir / "bin" / "hermes-poller"),
            "run",
        ],
    }
    return {
        name: {
            "Label": f"{LABEL_PREFIX}.{name}",
            "ProgramArguments": commands[name],
            "WorkingDirectory": str(project_dir),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "ProcessType": "Background",
            "StandardOutPath": str(log_dir / f"{name}.out.log"),
            "StandardErrorPath": str(log_dir / f"{name}.err.log"),
            **(
                {"EnvironmentVariables": {"REVIEW_UI_OPEN_BROWSER": "false"}}
                if name == "review"
                else {}
            ),
        }
        for name in SERVICE_NAMES
    }


def _launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("Mac background mode is only available on macOS")
    for path in (
        PROJECT_DIR / "bin" / "start-outbox-service",
        PROJECT_DIR / "bin" / "start-review-ui",
        PROJECT_DIR / "bin" / "hermes-poller",
        PROJECT_DIR / "macos" / "background_launcher.c",
        PROJECT_DIR / "config" / "local.env",
        PROJECT_DIR / "config" / "outbox-service.env",
    ):
        if not path.exists():
            raise RuntimeError(f"Required project file is missing: {path}")


def _wait_until_unloaded(domain: str, label: str) -> None:
    for _ in range(40):
        if _launchctl("print", f"{domain}/{label}", check=False).returncode:
            return
        time.sleep(0.05)
    raise RuntimeError(f"Timed out unloading {label}")


def _install_background_app() -> Path:
    executable = background_app_executable()
    contents = executable.parent.parent
    executable.parent.mkdir(parents=True, exist_ok=True)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(background_app_info(), stream, sort_keys=False)
    subprocess.run(
        [
            "/usr/bin/cc",
            "-Os",
            "-Wall",
            "-Wextra",
            str(PROJECT_DIR / "macos" / "background_launcher.c"),
            "-o",
            str(executable),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            "--identifier",
            BACKGROUND_BUNDLE_ID,
            str(contents.parent),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [str(LAUNCH_SERVICES_REGISTER), "-f", str(contents.parent)],
        check=True,
        capture_output=True,
        text=True,
    )
    return executable


def install() -> None:
    _require_macos()
    domain = f"gui/{os.getuid()}"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    log_dir = PROJECT_DIR / "outputs" / "macos-service"
    launch_agents.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    launcher = _install_background_app()
    for payload in service_payloads(log_dir=log_dir, launcher=launcher).values():
        label = payload["Label"]
        path = launch_agents / f"{label}.plist"
        _launchctl("bootout", f"{domain}/{label}", check=False)
        _wait_until_unloaded(domain, label)
        with path.open("wb") as stream:
            plistlib.dump(payload, stream, sort_keys=False)
        path.chmod(0o600)
        _launchctl("bootstrap", domain, str(path))
        print(f"installed {label}")


def uninstall() -> None:
    domain = f"gui/{os.getuid()}"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    for name in SERVICE_NAMES:
        label = f"{LABEL_PREFIX}.{name}"
        _launchctl("bootout", f"{domain}/{label}", check=False)
        (launch_agents / f"{label}.plist").unlink(missing_ok=True)
        print(f"removed {label}")
    app = background_app_executable().parent.parent.parent
    if app.name == BACKGROUND_APP_NAME:
        shutil.rmtree(app, ignore_errors=True)


def status() -> None:
    domain = f"gui/{os.getuid()}"
    for name in SERVICE_NAMES:
        label = f"{LABEL_PREFIX}.{name}"
        result = _launchctl("print", f"{domain}/{label}", check=False)
        if result.returncode:
            print(f"{name}: not installed")
            continue
        state = next(
            (
                line.split("=", 1)[1].strip()
                for line in result.stdout.splitlines()
                if line.strip().startswith("state =")
            ),
            "loaded",
        )
        print(f"{name}: {state}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage macOS background mode")
    parser.add_argument("action", choices=("install", "status", "uninstall"))
    args = parser.parse_args(argv)
    try:
        {"install": install, "status": status, "uninstall": uninstall}[args.action]()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
