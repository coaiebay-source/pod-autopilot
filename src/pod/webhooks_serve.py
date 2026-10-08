"""Entrypoint for the webhook receiver.

    PUBLIC_WEBHOOK_URL=https://yourstore.com/hooks/square \\
    uvicorn pod.webhooks:app --host 0.0.0.0 --port 8080

Bind 0.0.0.0, not 127.0.0.1: Square and Printful must reach this from the
internet. Put it behind TLS (Caddy or nginx) -- Square requires https for the
notification URL, and the signature is computed over that exact URL string.
"""
from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("pod.webhooks:app", host="0.0.0.0", port=8080, reload=False)


if __name__ == "__main__":
    main()
