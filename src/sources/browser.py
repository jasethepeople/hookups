from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SAFE_HEADERS = {
    "cache-control",
    "cf-mitigated",
    "content-type",
    "date",
    "etag",
    "last-modified",
    "location",
    "retry-after",
    "server",
}
CHALLENGE_MARKERS = (
    "cf-chl-",
    "challenge-platform",
    "just a moment",
    "verify you are human",
    "checking your browser",
    "captcha",
)
STEALTH_SCRIPT = """
Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""


def is_challenge(status: int, title: str, text: str, headers: dict | None = None) -> bool:
    if status in {403, 429, 503} or (headers or {}).get("cf-mitigated", "").lower() == "challenge":
        return True
    haystack = f"{title}\n{text[:20000]}".lower()
    return any(marker in haystack for marker in CHALLENGE_MARKERS)


def _safe_headers(headers) -> dict[str, str]:
    return {
        key: headers.get(key, "")
        for key in SAFE_HEADERS
        if headers.get(key) is not None
    }


def fetch_with_browser(
    url: str,
    user_data_dir: str | Path,
    deadline: float,
    proxy_url: str | None = None,
    fallback_urls: tuple[str, ...] = (),
) -> dict:
    remaining = max(1.0, deadline - time.monotonic())
    profile = Path(user_data_dir)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.chmod(0o700)
    if not os.environ.get("DISPLAY") and not os.environ.get("HOOKUPS_BROWSER_WORKER"):
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            payload = json.dumps({
                "url": url,
                "user_data_dir": str(profile),
                "seconds": remaining,
                "fallback_urls": list(fallback_urls),
            }, separators=(",", ":"))
            env = os.environ.copy()
            env["HOOKUPS_BROWSER_WORKER"] = "1"
            command = [xvfb, "-a", sys.executable, str(Path(__file__).resolve()), "--worker", payload]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=max(5, int(remaining) + 5),
                    env=env,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return {"status": 0, "final_url": url, "headers": {}, "body": "", "text": "", "challenge": True, "proxy_used": False, "proxy_required": not bool(proxy_url), "error": "browser_timeout"}
            if completed.returncode == 0:
                try:
                    return json.loads(completed.stdout)
                except json.JSONDecodeError:
                    pass
            return {"status": 0, "final_url": url, "headers": {}, "body": "", "text": "", "challenge": True, "proxy_used": False, "proxy_required": not bool(proxy_url), "error": "browser_worker_failed"}
    return _fetch_visible(url, profile, remaining, proxy_url, fallback_urls)


def _fetch_visible(url: str, profile: Path, seconds: float, proxy_url: str | None, fallback_urls: tuple[str, ...]) -> dict:
    deadline = time.monotonic() + max(1.0, seconds)
    attempts = []
    last_result = {
        "status": 0,
        "final_url": url,
        "headers": {},
        "body": "",
        "text": "",
        "challenge": True,
        "proxy_used": False,
        "proxy_required": not bool(proxy_url),
        "error": None,
    }

    def launch(target_url: str, proxy: str | None):
        options = {
            "headless": False,
            "locale": "en-US",
            "viewport": {"width": 1365, "height": 900},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if proxy:
            options["proxy"] = {"server": proxy}
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(profile), **options)
            try:
                context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
                result = _visit(context, target_url, deadline, proxy is not None)
                attempts.append(result)
                if result["challenge"]:
                    context.add_init_script(STEALTH_SCRIPT)
                    result = _visit(context, target_url, deadline, proxy is not None)
                    attempts.append(result)
                return result
            finally:
                context.close()

    try:
        result = launch(url, None)
        if not result["challenge"]:
            return result
        last_result = result
        if proxy_url:
            if time.monotonic() < deadline:
                time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
                result = launch(url, proxy_url)
                if not result["challenge"]:
                    return result
                last_result = result
        else:
            last_result["proxy_required"] = True
            last_result["error"] = "proxy_url_unset"
        for fallback_url in fallback_urls:
            if time.monotonic() >= deadline:
                break
            result = launch(fallback_url, proxy_url)
            if not result["challenge"]:
                result["final_url"] = fallback_url
                return result
            last_result = result
    except Exception as error:
        return {"status": 0, "final_url": url, "headers": {}, "body": "", "text": "", "challenge": False, "proxy_used": bool(proxy_url), "proxy_required": False, "error": f"browser_{type(error).__name__}"}
    last_result["challenge"] = True
    last_result["proxy_used"] = bool(proxy_url)
    last_result["proxy_required"] = not bool(proxy_url)
    if not last_result.get("error") and not proxy_url:
        last_result["error"] = "proxy_url_unset"
    return last_result


def _visit(context, url: str, deadline: float, proxy_used: bool) -> dict:
    page = context.new_page()
    status = 0
    headers = {}
    final_url = url
    try:
        remaining = max(1.0, deadline - time.monotonic())
        response = page.goto(url, wait_until="domcontentloaded", timeout=max(1000, min(30000, int(remaining * 1000))))
        if response is not None:
            status = response.status
            headers = _safe_headers(response.headers)
            final_url = response.url
        page.wait_for_timeout(min(15000, max(0, int((deadline - time.monotonic()) * 1000))))
        title = page.title()
        text = page.locator("body").inner_text(timeout=3000)
        body = page.content()
        challenged = is_challenge(status, title, text, headers)
        if challenged:
            page.wait_for_timeout(min(20000, max(0, int((deadline - time.monotonic()) * 1000))))
            title = page.title()
            text = page.locator("body").inner_text(timeout=3000)
            body = page.content()
            challenged = is_challenge(status, title, text, headers)
        return {"status": status, "final_url": final_url, "headers": headers, "body": body, "text": text, "challenge": challenged, "proxy_used": proxy_used, "proxy_required": False, "error": None}
    except PlaywrightTimeoutError:
        return {"status": status, "final_url": final_url, "headers": headers, "body": "", "text": "", "challenge": True, "proxy_used": proxy_used, "proxy_required": False, "error": "browser_navigation_timeout"}
    except Exception as error:
        return {"status": status, "final_url": final_url, "headers": headers, "body": "", "text": "", "challenge": False, "proxy_used": proxy_used, "proxy_required": False, "error": f"browser_{type(error).__name__}"}
    finally:
        page.close()


def _worker(payload: str) -> int:
    request = json.loads(payload)
    result = _fetch_visible(
        request["url"],
        Path(request["user_data_dir"]),
        float(request["seconds"]),
        os.environ.get("PROXY_URL", "").strip() or None,
        tuple(request.get("fallback_urls", [])),
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--worker":
    raise SystemExit(_worker(sys.argv[2]))
