"""HTTP security headers on every response (M16; closes known issues #7, #25).

- **Content-Security-Policy**: everything from this origin only (Bootstrap and
  the app's JS/CSS are vendored -- no CDN). ``script-src 'self'`` with **no**
  ``'unsafe-inline'``: the two inline handlers the templates had were moved
  into static/js/loading.js, so an injected ``<script>`` or ``onerror=``
  cannot run even if escaping were ever bypassed. ``style-src`` allows inline
  styles (a handful of ``style=""`` width/colour attributes) -- styles cannot
  execute code. ``img-src data:`` is for Bootstrap's inline SVG icons.
  ``frame-ancestors 'none'`` + ``X-Frame-Options: DENY`` stop clickjacking of
  the many one-click POST buttons (cancel, pay, refund, status).
- **Cache-Control: no-store** on every page served to a signed-in user, so a
  shared browser's back button or cache cannot show the previous user's
  orders, reservations or dashboards after logout.
- **HSTS** only when the session cookie is Secure (production), so local HTTP
  development is not pinned to HTTPS.
"""

from __future__ import annotations

from flask import Flask, Response, request, session

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


def register_security_headers(app: Flask) -> None:
    @app.after_request
    def _security_headers(response: Response) -> Response:
        headers = response.headers
        headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if app.config.get("SESSION_COOKIE_SECURE"):
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # Signed-in pages are personal; static assets are not.
        if request.endpoint != "static" and "user_id" in session:
            headers["Cache-Control"] = "no-store"
            headers.add("Vary", "Cookie")
        return response
