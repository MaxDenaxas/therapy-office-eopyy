"""Request filtering and security headers (applied to every request, API and frontend).

Order of checks (the first failure answers; nothing reaches a route):
  1. client   the TCP peer must be a loopback address (403). The app is bound to
              loopback, and this keeps it closed even if someone binds it wider.
  2. Host     the Host header must name a loopback host (400): stops DNS rebinding.
  3. Origin   unsafe methods (anything but GET/HEAD) need an Origin header equal to
              the request's own origin (403). With the Vite dev proxy
              (``changeOrigin: false``) browser and backend see the same origin, so
              no CORS is configured anywhere.
  4. body     an unsafe /api request with a body must be ``application/json`` (415),
              which also rules out HTML form posts.
Then the response gets the security headers.
"""

from __future__ import annotations

import ipaddress

from fastapi import FastAPI, Request
from starlette.datastructures import MutableHeaders

from app.config import LOOPBACK_HOSTS
from app.errors import error_response

SAFE_METHODS = frozenset({"GET", "HEAD"})
DOCS_PATHS = frozenset({"/api/docs", "/api/docs/oauth2-redirect"})

_COMMON = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
_API = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}
_FRONTEND_CSP = (
    "default-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def apply_security_headers(headers: MutableHeaders, api: bool, docs: bool = False) -> None:
    for name, value in _COMMON.items():
        headers[name] = value
    if api:
        headers["Cache-Control"] = _API["Cache-Control"]
        if not docs:  # the development-only Swagger page loads its own assets
            headers["Content-Security-Policy"] = _API["Content-Security-Policy"]
    else:
        headers.setdefault("Content-Security-Policy", _FRONTEND_CSP)


def is_loopback_client(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def host_name(host_header: str | None) -> str | None:
    """The host part of a Host header (``[::1]:8000`` → ``::1``), lower-case."""
    if not host_header:
        return None
    value = host_header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end == -1 or (len(value) > end + 1 and not value[end + 1 :].startswith(":")):
            return None
        return value[1:end]
    if value.count(":") > 1:
        return None
    return value.split(":", 1)[0]


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def install_security(app: FastAPI) -> None:
    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        api = _is_api(request.url.path)

        def refuse(status: int, code: str):
            response = error_response(status, code)
            apply_security_headers(response.headers, api=True)
            return response

        if not is_loopback_client(request.client.host if request.client else None):
            return refuse(403, "forbidden")
        host_header = request.headers.get("host")
        if host_name(host_header) not in LOOPBACK_HOSTS:
            return refuse(400, "invalid_host")
        if request.method not in SAFE_METHODS:
            origin = (request.headers.get("origin") or "").strip().lower()
            expected = f"{request.url.scheme}://{host_header.strip().lower()}"
            if origin != expected:
                return refuse(403, "origin_rejected")
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site is not None and fetch_site.lower() != "same-origin":
                return refuse(403, "origin_rejected")
            if api and _has_body(request):
                media_type = request.headers.get("content-type", "").split(";")[0].strip()
                if media_type.lower() != "application/json":
                    return refuse(415, "unsupported_media_type")

        response = await call_next(request)
        apply_security_headers(response.headers, api=api, docs=request.url.path in DOCS_PATHS)
        return response


def _has_body(request: Request) -> bool:
    if "transfer-encoding" in request.headers:
        return True
    length = request.headers.get("content-length")
    if length is None:
        return False
    try:
        return int(length) > 0
    except ValueError:
        return True
