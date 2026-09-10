import os

# Tests must never touch the developer's real browser cookie stores (slow,
# and can pop a macOS keychain prompt). `cookies_browser` now defaults to
# "auto"; this pins it off for the whole test run. Tests that specifically
# exercise cookie plumbing mock `cookie_header_for` directly.
os.environ.setdefault("COPITA_NO_BROWSER_COOKIES", "1")
