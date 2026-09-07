"""Wait up to three minutes for a real application readiness response."""
import json
import sys
import time
import urllib.error
import urllib.request

deadline = time.monotonic() + 180
while True:
    try:
        with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
            if response.status == 200:
                print('Application ready:', response.read().decode())
                break
    except (OSError, urllib.error.URLError):
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('Application did not become ready within 180 seconds')
    time.sleep(2)
