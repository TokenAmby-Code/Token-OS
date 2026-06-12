# AHK Runtime Cache Notes

This file is intentionally lightweight. Changes under `ahk/` are WSL-runtime relevant and should trigger the satellite `/runtime/refresh` path during deploy verification.

Proof note: this second line verifies the Mac token-restart process has the WSL refresh secret exported and calls `/runtime/refresh` instead of falling back to `/restart`.

Proof note: this third line verifies the patched WSL refresh helper fetches a fresh merge commit into the live checkout.
