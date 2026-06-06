[app]
# ── Identity ──────────────────────────────────────────────────────────────────
title          = Buddy Guard
package.name   = buddyguard
package.domain = com.parentalcontrol
version        = 1.0.0

# ── Source ────────────────────────────────────────────────────────────────────
source.dir          = .
source.include_exts = py,png,jpg,kv,atlas,json
source.exclude_dirs = tests,bin,.buildozer,.git,__pycache__

# ── Requirements ──────────────────────────────────────────────────────────────
# Explicit versions prevent p4a pulling incompatible builds at compile time.
# - python3==3.11.0        : must match python-version in GitHub Actions workflow
# - kivy==2.3.0            : last stable release; kivymd 1.2.0 requires exactly this
# - kivymd==1.2.0          : pinned — newer versions break kivy 2.3.0 API
# - requests==2.31.0       : used by ApiClient for all HTTP calls
# - certifi                : CA bundle for SSL verification on Android (no system store)
# - urllib3                : requests dependency — bundled explicitly for Android
# - charset-normalizer     : requests dependency
# - idna                   : requests dependency
# - android                : p4a Android bindings (activity, permissions)
# - plyer                  : cross-platform notifications / sensors helper
# - jnius                  : Java bridge — used for Camera, MediaRecorder, Build, etc.
#   NOTE: jnius is pulled in automatically by the android recipe but listed
#         explicitly here so p4a uses a known-good version with kivy 2.3.0
requirements = python3==3.11.0,kivy==2.3.0,kivymd==1.2.0,requests==2.31.0,certifi,urllib3,charset-normalizer,idna,android,plyer,jnius

# ── Display ───────────────────────────────────────────────────────────────────
orientation = portrait
fullscreen   = 0

# ── Android permissions ───────────────────────────────────────────────────────
# Every permission used in main.py is listed here.
# Missing any one of these causes a silent runtime failure on Android 10+.
android.permissions =
    INTERNET,
    ACCESS_NETWORK_STATE,
    CAMERA,
    RECORD_AUDIO,
    WRITE_EXTERNAL_STORAGE,
    READ_EXTERNAL_STORAGE,
    FOREGROUND_SERVICE,
    FOREGROUND_SERVICE_MICROPHONE,
    FOREGROUND_SERVICE_CAMERA,
    RECEIVE_BOOT_COMPLETED,
    VIBRATE,
    POST_NOTIFICATIONS,
    DISABLE_KEYGUARD,
    BIND_DEVICE_ADMIN

# ── Android SDK / NDK ─────────────────────────────────────────────────────────
android.api     = 33
android.minapi  = 26
android.sdk     = 33
android.ndk     = 25b
android.ndk_api = 21

# ── Build options ─────────────────────────────────────────────────────────────
android.archs        = arm64-v8a, armeabi-v7a
android.allow_backup = True
android.accept_sdk_license = True

# ── Entrypoint & features ─────────────────────────────────────────────────────
android.entrypoint    = org.kivy.android.PythonActivity
android.logcat_filters = *:S python:D

# Enables the foreground service so the polling thread survives
# when the child minimises the app (required for background poll)
android.foreground_service = True
android.foreground_service_notification_title = Buddy Guard
android.foreground_service_notification_description = Parental control active

# Private app storage — certifi CA bundle is read from here at runtime
android.private_storage = True

# ── Presplash / icon (optional — add your own files to replace defaults) ──────
# presplash.filename = %(source.dir)s/presplash.png
# icon.filename      = %(source.dir)s/icon.png

[buildozer]
log_level   = 2
warn_on_root = 1
