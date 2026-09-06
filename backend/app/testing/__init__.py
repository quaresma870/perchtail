"""Test-only support modules — never imported by the app itself except
through the explicit, opt-in PERCHTAIL_TEST_PATCH_MODULE hook in
app/main.py (see app/testing/fake_winrm.py). Nothing under this package is
reachable in a normal deployment."""
