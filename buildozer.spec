# ============================================================
# BUILDOZER.SPEC — Buddy Guard (Child Device APK)
# Fixed: requirements, SDK/NDK versions, permissions, foreground service
# ============================================================

[app]

# ── Identity ──────────────────────────────────────────────────────────────────
title          = Buddy Guard
package.name   = buddyguard
package.domain = com.parentalcontrol
version        = 1.0.0

source.dir          = .
source.include_exts = py,png,jpg,kv,atlas,json
source.exclude_dirs = tests,bin,.buildozer,.git,__pycache__,.github

# ── Requirements ──────────────────────────────────────────────────────────────
# IMPORTANT: keep jnius and android — they are required for camera, audio,
# notifications, and lock screen. DO NOT remove them.
# python3 (no version pin) lets p4a pick the version that matches the host.
# Cython 0.29.37 is the last 0.29.x that supports Python 3.10+.
# urllib3 1.26.18 pinned: later versions changed SSL behaviour on Android.
# ─────────────────────────────────────────────────────────────────────────────
requirements = python3,kivy==2.3.0,requests==2.31.0,certifi,urllib3==1.26.18,charset-normalizer,idna,android,plyer,jnius

orientation = portrait
fullscreen   = 0

# ── Permissions ───────────────────────────────────────────────────────────────
android.permissions =
    INTERNET,
    ACCESS_NETWORK_STATE,
    ACCESS_WIFI_STATE,
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
    BIND_DEVICE_ADMIN,
    REQUEST_INSTALL_PACKAGES

# ── SDK / NDK ─────────────────────────────────────────────────────────────────
android.api     = 33
android.minapi  = 26
android.ndk     = 25b
android.ndk_api = 21

# ── Build ─────────────────────────────────────────────────────────────────────
# Build both ABIs so the APK works on all Android phones (arm64 = modern,
# armeabi-v7a = older phones). Remove armeabi-v7a if APK size matters more.
android.archs = arm64-v8a, armeabi-v7a

android.allow_backup       = True
android.accept_sdk_license = True

# ── App features ──────────────────────────────────────────────────────────────
android.entrypoint     = org.kivy.android.PythonActivity
android.logcat_filters = *:S python:D

# Foreground service keeps the polling thread alive when app is minimised.
# Without this, Android kills the background thread after ~60 seconds.
android.foreground_service                        = True
android.foreground_service_notification_title     = Buddy Guard
android.foreground_service_notification_description = Parental monitoring active

[buildozer]
log_level    = 2
warn_on_root = 1
