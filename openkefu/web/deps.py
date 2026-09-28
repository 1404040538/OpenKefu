"""Stateless request helpers shared by routers."""

from __future__ import annotations

import ipaddress

from fastapi import HTTPException, Request

MIN_USER_PASSWORD_LENGTH = 12
MAX_USER_PASSWORD_LENGTH = 256


def client_ip(request: Request) -> str:
    peer = str(request.client.host if request.client else "unknown")
    # Only a same-host reverse proxy may supply forwarding metadata. Taking the
    # last entry prevents a client-provided prefix from spoofing a loopback IP
    # when Nginx appends the actual remote address.
    if is_loopback_ip(peer):
        forwarded = request.headers.get("x-forwarded-for") or ""
        last = forwarded.rsplit(",", 1)[-1].strip()
        if last:
            return last
    return peer


def is_loopback_ip(ip: str) -> bool:
    if str(ip or "").strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(ip or "").strip()).is_loopback
    except ValueError:
        return False


def validate_user_password(password: str) -> None:
    length = len(str(password or ""))
    if length < MIN_USER_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"password must be at least {MIN_USER_PASSWORD_LENGTH} characters",
        )
    if length > MAX_USER_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"password must be at most {MAX_USER_PASSWORD_LENGTH} characters",
        )
