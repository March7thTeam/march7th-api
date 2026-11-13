"""Legacy main-module patch seams for integrations predating route extraction."""
import sys
from app.auth import authenticate_user
from app.email_verification import send_verification_email

_ORIGINALS = {"authenticate_user": authenticate_user, "send_verification_email": send_verification_email}


def legacy_override(name, default):
    legacy = getattr(sys.modules.get("app.main"), name, None)
    return legacy if legacy is not None and legacy is not _ORIGINALS.get(name) else default
