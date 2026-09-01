#!/usr/bin/env python3
"""Check the user-facing services through the Nginx reverse proxy."""

from __future__ import annotations

import argparse
import os
import ssl
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

DEFAULT_BASE_URL = os.environ.get("HEALTHCHECK_BASE_URL", "https://nginx")
DEFAULT_TIMEOUT = 5.0
DEFAULT_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = 1.0

ENDPOINTS = (
    ("nginx", "/healthz"),
    ("gitea", "/gitea/"),
    ("woodpecker", "/woodpecker/"),
    ("prometheus", "/prometheus/"),
    ("grafana", "/grafana/"),
)


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new):
        return None


OPENER = build_opener(NoRedirectHandler)
INSECURE_HTTPS_OPENER = build_opener(
    NoRedirectHandler,
    HTTPSHandler(context=ssl._create_unverified_context()),
)


def request_status(url: str, timeout: float) -> int:
    request = Request(url, headers={"Accept": "*/*", "User-Agent": "infra-healthcheck/1"})
    opener = INSECURE_HTTPS_OPENER if url.startswith("https://") else OPENER
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def status_is_expected(path: str, status: int) -> bool:
    if path == "/healthz":
        return status == 200
    return 200 <= status < 400


def check_endpoint(
    base_url: str,
    name: str,
    path: str,
    *,
    attempts: int,
    timeout: float,
    retry_delay: float,
    sleeper: Callable[[float], None] = time.sleep,
) -> str | None:
    url = f"{base_url.rstrip('/')}{path}"
    last_error = "no response"
    for attempt in range(1, attempts + 1):
        try:
            status = request_status(url, timeout)
            if status_is_expected(path, status):
                return None
            last_error = f"HTTP {status}"
        except (OSError, TimeoutError, URLError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            sleeper(retry_delay)
    return f"{name} {path}: {last_error} after {attempts} attempt(s)"


def run_checks(
    base_url: str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[str]:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as executor:
        futures = [
            executor.submit(
                check_endpoint,
                base_url,
                name,
                path,
                attempts=attempts,
                timeout=timeout,
                retry_delay=retry_delay,
                sleeper=sleeper,
            )
            for name, path in ENDPOINTS
        ]
        return [error for future in futures if (error := future.result())]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check services through the Nginx reverse proxy")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY)
    args = parser.parse_args(argv)

    try:
        errors = run_checks(
            args.base_url,
            attempts=args.attempts,
            timeout=args.timeout,
            retry_delay=args.retry_delay,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if errors:
        print("healthcheck failed:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"healthcheck passed: {len(ENDPOINTS)} Nginx proxy endpoints")
    return 0


if __name__ == "__main__":
    sys.exit(main())
