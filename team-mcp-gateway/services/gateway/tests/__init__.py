"""Shared by the test modules."""

from cryptography.fernet import Fernet

# The gateway encrypts members' upstream credentials with this key in tests.
KEY = Fernet.generate_key().decode()
