#!/usr/bin/env python3
"""Probe google.com.hk with undetected-chromedriver and classify CAPTCHA state.

The probe launches Chrome through undetected-chromedriver, visits a normal
Google warmup search and an AI Mode search, then emits a JSON summary. It is a
live diagnostic harness, not a solver: success means Google did not present a
CAPTCHA challenge and AI Mode tokens were present on the AI Mode page.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

CAPTCHA_TEXT_PATTERNS = [
    "our systems have detected unusual traffic",
    "unusual traffic from your computer network",
    "to continue, please type the characters below",
    "about this page appears when google automatically detects requests",
]

CAPTCHA_HTML_PATTERNS = [
    r'id=["\']captcha["\']',
    r'id=["\']captcha-form["\']',
    r'action=["\'][^"\']*/sorry/(?:index|sorry)?',
    r'class=["\'][^"\']*g-recaptcha',
    r'https?://www\.google\.com/recaptcha/api',
]

TOKEN_PATTERNS = {
    "data_srtst": r'data-srtst="([^"]+)"',
    "data_xsrf_folwr_token": r'data-xsrf-folwr-token="([^"]+)"',
    "data_garc": r'data-garc="([^"]+)"',
    "data_lro_token": r'data-lro-token="([^"]+)"',
    "data_lro_signature": r'data-lro-signature="([^"]+)"',
}


def _iter_existing(paths: list[Path]) -> list[str]:
    return [str(path) for path in paths if path.is_file()]


def _which_chrome() -> str | None:
    env = os.environ.get("UC_CHROME_BINARY") or os.environ.get("CHROME_PATH")
    candidates: list[str] = [env] if env else []
    system = platform.system()
    home = Path.home()

    if system == "Windows":
        for key in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base_value = os.environ.get(key)
            if not base_value:
                continue
            base = Path(base_value)
            candidates.extend(
                _iter_existing(
                    [
                        base / "Google" / "Chrome" / "Application" / "chrome.exe",
                        base / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    ]
                )
            )
            candidates.extend(
                str(path)
                for path in sorted(
                    (base / "agent-browser-cli" / "chrome-for-testing").glob("*/chrome-win64/chrome.exe"),
                    reverse=True,
                )
            )
        candidates.extend(["chrome.exe", "msedge.exe"])
    elif system == "Darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ]
        )
    else:
        candidates.extend(
            str(path)
            for root in [
                home / ".local/share/browser-binaries/puppeteer/chrome",
                home / ".local/share/browser-binaries/ms-playwright",
            ]
            for path in sorted(root.glob("**/chrome-linux64/chrome"), reverse=True)
        )
        candidates.extend(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"])

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file() and (system == "Windows" or os.access(path, os.X_OK)):
            return str(path)
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _chrome_version(binary: str) -> dict[str, Any]:
    path_match = re.search(r"(?:^|[\\/])(\d+)\.\d+\.\d+\.\d+(?:[\\/]|$)", binary or "")
    path_major = int(path_match.group(1)) if path_match else None
    try:
        cp = subprocess.run([binary, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    except Exception as exc:  # noqa: BLE001 - diagnostic harness
        return {"ok": False, "error": repr(exc), "major": path_major}
    text = (cp.stdout or cp.stderr).strip()
    match = re.search(r"(\d+)\.\d+\.\d+\.\d+", text)
    return {"ok": cp.returncode == 0, "raw": text, "major": int(match.group(1)) if match else path_major}


def _proxy_open(proxy: str) -> bool | None:
    match = re.match(r"^(?:socks5h?|https?)://([^:/]+):(\d+)$", proxy)
    if not match:
        return None
    sock = socket.socket()
    sock.settimeout(1.0)
    try:
        sock.connect((match.group(1), int(match.group(2))))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _classify(html: str, url: str, title: str) -> dict[str, Any]:
    lower_html = (html or "").lower()
    lower_url = (url or "").lower()
    parsed = urlparse(url or "")

    hits: list[str] = []
    if parsed.path.startswith("/sorry/"):
        hits.append("url:/sorry/")
    if "google.com/sorry/" in lower_url or "google.com.hk/sorry/" in lower_url:
        hits.append("url:google_sorry")
    for pattern in CAPTCHA_TEXT_PATTERNS:
        if pattern in lower_html:
            hits.append(f"text:{pattern}")
    for pattern in CAPTCHA_HTML_PATTERNS:
        if re.search(pattern, html or "", flags=re.IGNORECASE):
            hits.append(f"html:{pattern}")

    tokens = {name: bool(re.search(pattern, html or "")) for name, pattern in TOKEN_PATTERNS.items()}
    return {
        "captcha": bool(hits),
        "captcha_hits": hits,
        "sorry_url": parsed.path.startswith("/sorry/"),
        "tokens": tokens,
        "token_count": sum(1 for value in tokens.values() if value),
        "has_ai_mode_token": bool(tokens.get("data_srtst")),
        "html_length": len(html or ""),
        "title": title,
        "preview": re.sub(r"\s+", " ", html or "")[:500],
    }


def _emit(payload: dict[str, Any], out_json: str) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out_json:
        out = Path(out_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="what is 7*8 answer only the number")
    parser.add_argument("--proxy", default=os.environ.get("GEMINI_SEARCH_PROXY_SERVER", "socks5://127.0.0.1:7897"))
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless. Headed mode is the default.")
    parser.add_argument("--out-json", default="")
    parser.add_argument("--artifact-dir", default="")
    parser.add_argument("--keep-profile", action="store_true")
    parser.add_argument("--driver-executable-path", default=os.environ.get("GEMINI_SEARCH_CHROMEDRIVER") or os.environ.get("UC_CHROMEDRIVER", ""))
    parser.add_argument("--timeout", type=int, default=45)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    artifact_dir = Path(args.artifact_dir) if args.artifact_dir else Path(tempfile.mkdtemp(prefix="uc-google-probe-artifacts-"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(tempfile.mkdtemp(prefix="uc-google-profile-"))
    chrome_binary = _which_chrome()

    result: dict[str, Any] = {
        "ok": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "probe": "undetected_chromedriver_google_captcha",
        "inputs": {
            "query": args.query,
            "proxy": args.proxy,
            "headless": args.headless,
            "artifact_dir": str(artifact_dir),
            "profile_dir": str(profile_dir),
        },
        "environment": {
            "python": sys.version,
            "display": os.environ.get("DISPLAY"),
            "chrome_binary": chrome_binary,
            "chrome_version": _chrome_version(chrome_binary) if chrome_binary else None,
            "driver_executable_path": args.driver_executable_path or None,
            "driver_exists": bool(args.driver_executable_path and Path(args.driver_executable_path).is_file()),
            "proxy_open": _proxy_open(args.proxy),
        },
        "stages": [],
        "artifacts": {},
    }

    if not chrome_binary:
        result["error"] = "chrome_binary_not_found"
        _emit(result, args.out_json)
        return 2

    driver = None
    try:
        import undetected_chromedriver as uc
        from selenium.webdriver.support.ui import WebDriverWait

        chrome_version = result["environment"].get("chrome_version") or {}
        version_main = chrome_version.get("major")
        options = uc.ChromeOptions()
        options.binary_location = chrome_binary
        options.add_argument(f"--proxy-server={args.proxy}")
        options.add_argument("--lang=en-US,en")
        options.add_argument("--window-size=1365,900")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--no-first-run")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        if args.headless:
            options.add_argument("--headless=new")

        start = time.time()
        driver = uc.Chrome(
            options=options,
            user_data_dir=str(profile_dir),
            driver_executable_path=args.driver_executable_path or None,
            browser_executable_path=chrome_binary,
            version_main=version_main,
            use_subprocess=True,
        )
        driver.set_page_load_timeout(args.timeout)
        result["stages"].append({"name": "launch", "ok": True, "elapsed_sec": round(time.time() - start, 3)})

        warmup_url = "https://www.google.com.hk/search?q=hello&hl=en&gl=us"
        start = time.time()
        driver.get(warmup_url)
        WebDriverWait(driver, args.timeout).until(lambda browser: browser.execute_script("return document.readyState") in ("interactive", "complete"))
        time.sleep(2)
        warmup_html = driver.page_source or ""
        warmup = {
            "name": "warmup_search",
            "ok": True,
            "elapsed_sec": round(time.time() - start, 3),
            "url": driver.current_url,
            "title": driver.title,
            "classification": _classify(warmup_html, driver.current_url, driver.title),
        }
        result["stages"].append(warmup)

        ai_url = "https://www.google.com.hk/search?q=" + quote(args.query, safe="") + "&hl=en&gl=us&udm=50&aep=1&ntc=1"
        start = time.time()
        driver.get(ai_url)
        WebDriverWait(driver, args.timeout).until(lambda browser: browser.execute_script("return document.readyState") in ("interactive", "complete"))
        time.sleep(5)
        try:
            driver.execute_script("window.scrollTo(0, Math.min(document.body.scrollHeight, 1200));")
            time.sleep(1)
        except Exception:
            pass
        ai_html = driver.page_source or ""
        ai = {
            "name": "ai_mode_search",
            "ok": True,
            "elapsed_sec": round(time.time() - start, 3),
            "url": driver.current_url,
            "title": driver.title,
            "classification": _classify(ai_html, driver.current_url, driver.title),
        }
        result["stages"].append(ai)

        html_path = artifact_dir / "ai_mode_page.html"
        html_path.write_text(ai_html, encoding="utf-8", errors="replace")
        result["artifacts"]["ai_mode_html"] = str(html_path)
        screenshot_path = artifact_dir / "ai_mode_page.png"
        try:
            driver.save_screenshot(str(screenshot_path))
            result["artifacts"]["ai_mode_screenshot"] = str(screenshot_path)
        except Exception as exc:  # noqa: BLE001 - diagnostic harness
            result["artifacts"]["screenshot_error"] = repr(exc)

        warmup_cls = warmup["classification"]
        ai_cls = ai["classification"]
        result["captcha"] = bool(warmup_cls["captcha"] or ai_cls["captcha"])
        result["successful_for_engine_integration"] = not result["captcha"] and ai_cls["has_ai_mode_token"]
        result["ok"] = not result["captcha"]
        if result["captcha"]:
            result["error"] = "captcha_detected"
        elif not ai_cls["has_ai_mode_token"]:
            result["error"] = "no_ai_mode_token"
        else:
            result["error"] = "none"
    except Exception as exc:  # noqa: BLE001 - diagnostic harness
        result["error"] = "probe_exception"
        result["exception"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        try:
            if driver is not None:
                driver.quit()
        except Exception as exc:  # noqa: BLE001 - diagnostic harness
            result.setdefault("cleanup_errors", []).append(repr(exc))
        if args.keep_profile:
            result["artifacts"]["profile_dir"] = str(profile_dir)
        else:
            shutil.rmtree(profile_dir, ignore_errors=True)

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    _emit(result, args.out_json)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
