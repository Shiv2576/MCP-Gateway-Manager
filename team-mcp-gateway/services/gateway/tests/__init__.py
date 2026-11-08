"""Shared by the test modules."""

import base64
import os

from cryptography.fernet import Fernet

# The gateway encrypts members' upstream credentials with this key in tests.
KEY = Fernet.generate_key().decode()

# Seals the edge's requestState (32 random bytes, base64: what `openssl rand -base64 32` gives).
STATE_KEY = base64.b64encode(os.urandom(32)).decode()
