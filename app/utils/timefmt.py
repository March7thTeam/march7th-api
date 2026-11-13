from datetime import datetime, timezone


def now_iso(*, timespec: str = "seconds") -> str:
    return datetime.now(timezone.utc).isoformat(timespec=timespec)
