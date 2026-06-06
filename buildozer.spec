[app]
# ── Identity ──────────────────────────────────────────────────────────────────
title          = Buddy Guard
package.name   = buddyguard
package.domain = com.parentalcontrol
version        = 1.0.0
source.dir          = .
source.include_exts = py,png,jpg,kv,atlas,json,pem,crt
source.exclude_dirs = tests,bin,.buildozer,.git,__pycache__,.github

# ── Requirements ──────────────────────────────────────────────────────────────
requirements = python3==3.11.0,kivy==2.3.0,requests==2.31.0,certifi,urllib3==1.26.18,charset-normalizer,idna,android,plyer,jnius

orientation = portrait
fullscreen  = 0

# ── Services (background process) ─────────────────────────────────────────────
# Guard = service name, service.py = entry point
# This is what makes the app run in background after closing
services = Guard:service.py

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
    WAKE_LOCK,
    DISABLE_KEYGUARD,
    REQUEST_INSTALL_PACKAGES

# ── SDK / NDK ─────────────────────────────────────────────────────────────────
android.api     = 33
android.minapi  = 26
android.sdk     = 33
android.ndk     = 25b
android.ndk_api = 21

# ── Build ─────────────────────────────────────────────────────────────────────
android.archs              = arm64-v8a,armeabi-v7a
android.allow_backup       = True
android.accept_sdk_license = True
android.private_storage    = True

# ── App features ──────────────────────────────────────────────────────────────
android.entrypoint     = org.kivy.android.PythonActivity
android.logcat_filters = *:S python:D

# Foreground service notification (shown in status bar while service runs)
android.foreground_service                            = True
android.foreground_service_notification_title         = Buddy Guard
android.foreground_service_notification_description   = Parental monitoring active

[buildozer]
log_level    = 2
warn_on_root = 1
