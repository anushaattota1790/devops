import os
import ssl
import sys
import urllib.request


def main() -> int:
    tls = os.getenv("ENABLE_TLS", "false").lower() == "true"
    url = f"{'https' if tls else 'http'}://127.0.0.1:{os.getenv('PORT', '8080')}/healthz"
    context = ssl._create_unverified_context() if tls else None  # nosec B323 - loopback, self-signed
    try:
        with urllib.request.urlopen(url, timeout=3, context=context) as resp:  # nosec B310
            return 0 if resp.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
