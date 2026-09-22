"""Test-only credentials. Importable from any test without side effects.

Importing from conftest instead would run the test database setup a second time.
"""

# One password for every test account. Tests never use real or published passwords.
TEST_PASSWORD = "test-password-not-real"
