"""Generate a Web Push key into a private file without printing its value."""

import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec

from .notifications import b64


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m context_agent.push_keys /private/path/push.env")
    path = Path(sys.argv[1])
    key = (
        ec.generate_private_key(ec.SECP256R1()).private_numbers().private_value.to_bytes(32, "big")
    )
    # Exclusive create prevents accidentally rotating a deployed key.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        file.write(f"PUSH_VAPID_PRIVATE_KEY={b64(key)}\nPUSH_VAPID_SUBJECT=\n")
    print(f"Created private configuration file: {path}. Set its contact subject before use.")


if __name__ == "__main__":
    main()
