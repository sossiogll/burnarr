"""HTTP Basic auth via environment variables."""
from __future__ import annotations

import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()


def _expected_credentials() -> tuple[str, str]:
    user = os.environ.get("ADMIN_USER", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    return user, password


def require_auth(
    credentials: HTTPBasicCredentials = Depends(security),
) -> str:
    expected_user, expected_password = _expected_credentials()

    if not expected_user or not expected_password:
        # Misconfiguration: refuse to run unauthenticated.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_USER/ADMIN_PASSWORD non configurati sul server.",
        )

    user_ok = secrets.compare_digest(credentials.username, expected_user)
    pass_ok = secrets.compare_digest(credentials.password, expected_password)

    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenziali non valide.",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username
