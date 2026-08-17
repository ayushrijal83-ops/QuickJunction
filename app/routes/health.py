"""Liveness endpoint.

Deliberately reveals nothing: no version, no environment name, no database
state, no host details. It answers one question -- is the process serving
requests -- and is safe to expose to a load balancer or an uptime monitor.
"""

from __future__ import annotations

from flask import Blueprint, jsonify

health_bp = Blueprint("health", __name__)


@health_bp.get("/health")
def health():
    return jsonify(status="ok")
