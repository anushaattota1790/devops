import os

import uvicorn

from app.tls import generate_self_signed_cert


def main() -> None:
    options: dict[str, object] = {
        "host": os.getenv("HOST", "0.0.0.0"),  # nosec B104 - container must listen on all interfaces
        "port": int(os.getenv("PORT", "8080")),
    }
    if os.getenv("ENABLE_TLS", "false").lower() == "true":
        options["ssl_certfile"], options["ssl_keyfile"] = generate_self_signed_cert()
    uvicorn.run("app.main:app", **options)


if __name__ == "__main__":
    main()
