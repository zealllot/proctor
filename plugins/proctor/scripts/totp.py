"""TOTP code generator for PRoctor's form_with_totp auth flow.

Given a base32-encoded TOTP seed (the long string under the QR code at
2FA setup), prints the current 6-digit RFC 6238 TOTP code to stdout.

Usage:
    python3 totp.py <BASE32_SEED>

Implementation note: pure-stdlib HMAC-SHA1 + base32 decode, no third-party
deps. The 30-second time step and 6-digit output are RFC 6238 defaults,
matching Google Authenticator / Authy / 1Password / qor-auth's totp
provider. If a consumer's app uses a non-default step or digit count
this helper will need a flag — but the four big TOTP implementations
all agree on the defaults, so it's not in v0.3.0 scope.
"""

import base64
import hashlib
import hmac
import struct
import sys
import time


def code(seed_b32: str, at_unix: int | None = None) -> str:
    """Compute the 6-digit TOTP code for `seed_b32` at the given Unix time
    (defaults to now). Implements RFC 6238 with the standard 30-second step
    and SHA-1 HMAC. Padding-tolerant: accepts seeds with or without `=`
    padding (Google's QR strings usually omit it)."""
    if at_unix is None:
        at_unix = int(time.time())
    seed_b32 = seed_b32.strip().upper().replace(" ", "")
    pad = (-len(seed_b32)) % 8
    key = base64.b32decode(seed_b32 + ("=" * pad))
    counter = struct.pack(">Q", at_unix // 30)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python3 totp.py <BASE32_SEED>", file=sys.stderr)
        return 2
    print(code(sys.argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
