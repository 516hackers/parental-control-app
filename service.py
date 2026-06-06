# ============================================================
# service.py — Buddy Guard Background Service Entry Point
#
# This file is the entry point for the Android background
# service. Buildozer packages it separately from main.py.
#
# Place this file in the same folder as main.py.
# In buildozer.spec add:
#   services = Guard:service.py
# ============================================================

# Import and run the service loop from main.py
from main import run_as_service
run_as_service()
