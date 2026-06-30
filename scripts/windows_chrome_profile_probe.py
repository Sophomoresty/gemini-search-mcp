#!/usr/bin/env python3
"""Probe Windows Chrome persistent user-data-dir behavior for Google CAPTCHA.

The script launches a normal Chrome subprocess with a caller-supplied profile,
connects via raw CDP, and records whether Google warmup lands on /sorry/.
It can run a headed priming pass, a headless reuse pass, or both.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass
class ProbeResult:
    ok: bool
    mode: str
    headless: bool
    profile_dir: str
    port: int
    chrome_path: str
    url: str | None = None
    title: str | None = None
    captcha: bool | None = None
    cookie_count: int | None = None
    error: str | None = None
    elapsed_sec: float | None = None
    pid: int | None = None
    note: str | None = None


def _default_chrome_candidates() -> list[str]:
    candidates: list[str] = []
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(key)
        if not base:
            continue
        candidates.extend(
            [
                str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"),
                str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            ]
        )
    candidates.extend(["chrome.exe", "msedge.exe"])
    return candidates


def find_chrome(explicit: str | None = None) -> str:
    candidates = [explicit] if explicit else []
    candidates.extend(_default_chrome_candidates())
    for item in candidates:
        if not item:
            continue
        p = Path(item)
        if p.is_file():
            return str(p)
        found = shutil.which(item)
        if found:
            return found
    raise RuntimeError("Chrome/Edge not found; pass --chrome-path or set CHROME_PATH")


def http_json(url: str, timeout: float = 2.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def http_text(url: str, timeout: float = 10.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def wait_for_cdp(port: int, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            return http_json(f"http://127.0.0.1:{port}/json/version", timeout=1.5)
        except Exception as exc:  # noqa: BLE001 - diagnostic script
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"CDP did not become ready on port {port}: {last_error}")


def launch_chrome(
    chrome_path: str,
    profile_dir: Path,
    port: int,
    headless: bool,
    url: str,
    extra_args: Iterable[str] = (),
) -> subprocess.Popen[Any]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
    ]
    if headless:
        args.append("--headless=new")
    else:
        args.extend([
            "--new-window",
            "--window-position=80,80",
            "--window-size=1280,900",
        ])
    args.extend(extra_args)
    args.append(url)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def list_pages(port: int) -> list[dict[str, Any]]:
    return http_json(f"http://127.0.0.1:{port}/json/list", timeout=2.0)


def activate_first_page(port: int) -> dict[str, Any] | None:
    pages = [p for p in list_pages(port) if p.get("type") == "page"]
    if not pages:
        return None
    page = pages[0]
    target_id = page.get("id")
    if target_id:
        try:
            http_text(f"http://127.0.0.1:{port}/json/activate/{target_id}", timeout=1.0)
        except Exception:
            pass
    return page


def wait_for_google_result(port: int, timeout: float, expect_manual: bool) -> tuple[str | None, str | None, bool | None]:
    deadline = time.time() + timeout
    last_url: str | None = None
    last_title: str | None = None
    while time.time() < deadline:
        page = activate_first_page(port)
        if page:
            last_url = page.get("url") or last_url
            last_title = page.get("title") or last_title
            is_captcha = "/sorry/" in (last_url or "")
            if is_captcha and expect_manual:
                # Keep polling so the user can solve CAPTCHA in the visible window.
                time.sleep(2.0)
                continue
            if is_captcha or "google." in (last_url or ""):
                return last_url, last_title, is_captcha
        time.sleep(1.0)
    if last_url is None:
        return None, None, None
    return last_url, last_title, "/sorry/" in last_url


def get_cookie_count(port: int) -> int | None:
    try:
        encoded = urllib.parse.quote("https://www.google.com.hk/", safe="")
        cookies = http_json(f"http://127.0.0.1:{port}/json/cookies/{encoded}", timeout=2.0)
        if isinstance(cookies, list):
            return len(cookies)
    except Exception:
        return None
    return None


def kill_process(proc: subprocess.Popen[Any] | None, grace_sec: float = 3.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=grace_sec)


def run_probe(args: argparse.Namespace, mode: str, headless: bool, expect_manual: bool) -> ProbeResult:
    started = time.time()
    chrome_path = find_chrome(args.chrome_path or os.environ.get("CHROME_PATH"))
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    url = args.url
    proc: subprocess.Popen[Any] | None = None
    try:
        proc = launch_chrome(
            chrome_path=chrome_path,
            profile_dir=profile_dir,
            port=args.port,
            headless=headless,
            url=url,
            extra_args=args.chrome_arg,
        )
        wait_for_cdp(args.port, args.startup_timeout)
        if args.open_debug_page:
            webbrowser.open(f"http://127.0.0.1:{args.port}/json/version")
        final_url, title, captcha = wait_for_google_result(
            args.port,
            timeout=args.manual_timeout if expect_manual else args.probe_timeout,
            expect_manual=expect_manual,
        )
        cookie_count = get_cookie_count(args.port)
        ok = captcha is False or (expect_manual and captcha is not True)
        note = None
        if expect_manual and captcha is True:
            ok = False
            note = "CAPTCHA was still present when manual timeout expired."
        return ProbeResult(
            ok=ok,
            mode=mode,
            headless=headless,
            profile_dir=str(profile_dir),
            port=args.port,
            chrome_path=chrome_path,
            url=final_url,
            title=title,
            captcha=captcha,
            cookie_count=cookie_count,
            elapsed_sec=round(time.time() - started, 3),
            pid=proc.pid,
            note=note,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        return ProbeResult(
            ok=False,
            mode=mode,
            headless=headless,
            profile_dir=str(profile_dir),
            port=args.port,
            chrome_path=chrome_path if "chrome_path" in locals() else "",
            error=f"{type(exc).__name__}: {exc}",
            elapsed_sec=round(time.time() - started, 3),
            pid=proc.pid if proc else None,
        )
    finally:
        if not args.keep_open:
            kill_process(proc)


def write_json(path: str | None, data: dict[str, Any]) -> None:
    if not path:
        return
    out = Path(path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", required=True, help="Persistent Chrome user-data-dir to create/reuse")
    parser.add_argument("--mode", choices=["headed", "headless", "two-phase"], default="two-phase")
    parser.add_argument("--chrome-path", default=None)
    parser.add_argument("--port", type=int, default=19250)
    parser.add_argument("--url", default="https://www.google.com.hk/search?q=hello&hl=en&gl=us")
    parser.add_argument("--startup-timeout", type=float, default=20)
    parser.add_argument("--probe-timeout", type=float, default=25)
    parser.add_argument("--manual-timeout", type=float, default=180)
    parser.add_argument("--between-delay", type=float, default=2)
    parser.add_argument("--keep-open", action="store_true", help="Leave Chrome running after the selected phase")
    parser.add_argument("--open-debug-page", action="store_true")
    parser.add_argument("--chrome-arg", action="append", default=[], help="Additional raw Chrome arg; repeatable")
    parser.add_argument("--out", default=None, help="Write JSON summary to this path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    stages: dict[str, Any] = {}
    if args.mode in {"headed", "two-phase"}:
        headed = run_probe(args, "headed_prime", headless=False, expect_manual=True)
        stages["headed_prime"] = asdict(headed)
        if args.mode == "headed":
            data = {"ok": headed.ok, "stages": stages}
            write_json(args.out, data)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0 if headed.ok else 1
        if not headed.ok:
            data = {"ok": False, "stages": stages}
            write_json(args.out, data)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 1
        time.sleep(args.between_delay)

    if args.mode in {"headless", "two-phase"}:
        headless = run_probe(args, "headless_reuse", headless=True, expect_manual=False)
        stages["headless_reuse"] = asdict(headless)

    final_headless = stages.get("headless_reuse")
    ok = bool(final_headless.get("ok")) if final_headless else all(s.get("ok") for s in stages.values())
    data = {"ok": ok, "stages": stages}
    write_json(args.out, data)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
