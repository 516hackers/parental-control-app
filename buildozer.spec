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
# jnius    : Java bridge — explicitly listed so p4a uses a compatible version
# certifi  : CA bundle — the .pem file is bundled inside the APK
# urllib3  : pinned to avoid SSL behaviour changes in newer versions
# ─────────────────────────────────────────────────────────────────────────────
requirements = python3==3.11.0,kivy==2.3.0,requests==2.31.0,certifi,urllib3==1.26.18,charset-normalizer,idna,android,plyer,jnius

orientation = portrait
fullscreen  = 0

# ── Permissions ───────────────────────────────────────────────────────────────
android.permissions =
    INTERNET,
    ACCESS_NETWORK_STATE,
    ACCESS_WIFI_STATE,
    CAMERA,
    RECORD_AUDIO,
    WRITE_EXTERNAL_STORAGE,
    READ_EXTERNAL_STORAGE,
    MANAGE_EXTERNAL_STORAGE,
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
android.sdk     = 33
android.ndk     = 25b
android.ndk_api = 21

# ── Build ─────────────────────────────────────────────────────────────────────
android.archs              = arm64-v8a, armeabi-v7a
android.allow_backup       = True
android.accept_sdk_license = True
android.private_storage    = True

# ── App features ──────────────────────────────────────────────────────────────
android.entrypoint     = org.kivy.android.PythonActivity
android.logcat_filters = *:S python:D

# Foreground service — keeps polling thread alive when app is minimised
android.foreground_service                        = True
android.foreground_service_notification_title     = Buddy Guard
android.foreground_service_notification_description = Parental monitoring active

# ── KivyMD removed from requirements ─────────────────────────────────────────
# main.py no longer imports kivymd so it is not listed.
# If you add KivyMD widgets back, add: kivymd==1.2.0 to requirements above.

[buildozer]
log_level    = 2
warn_on_root = 1
