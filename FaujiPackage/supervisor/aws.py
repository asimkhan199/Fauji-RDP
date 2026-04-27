"""Best-effort EC2 public IP detection via IMDSv2.
Falls back to an external echo service, then to None."""
from __future__ import annotations
import urllib.request


def _imds_token() -> str | None:
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return r.read().decode("utf-8")
    except Exception:
        return None


def public_ip() -> str | None:
    tok = _imds_token()
    if tok:
        try:
            req = urllib.request.Request(
                "http://169.254.169.254/latest/meta-data/public-ipv4",
                headers={"X-aws-ec2-metadata-token": tok},
            )
            with urllib.request.urlopen(req, timeout=1.5) as r:
                ip = r.read().decode("utf-8").strip()
                return ip or None
        except Exception:
            pass
    # Fallback (works even off AWS)
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                ip = r.read().decode("utf-8").strip()
                if ip:
                    return ip
        except Exception:
            continue
    return None
