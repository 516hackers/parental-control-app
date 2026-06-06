[app]
title = Buddy Guard
package.name = buddyguard
package.domain = com.parentalcontrol

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json

version = 1.0.0

requirements = python3==3.11.0,kivy==2.3.0,kivymd,requests,certifi,urllib3,charset-normalizer,idna,android,plyer

orientation = portrait
fullscreen = 0

android.permissions = CAMERA,RECORD_AUDIO,FOREGROUND_SERVICE,RECEIVE_BOOT_COMPLETED,VIBRATE,POST_NOTIFICATIONS,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,INTERNET,ACCESS_NETWORK_STATE

android.api = 33
android.minapi = 26
android.ndk = 25b
android.sdk = 33
android.ndk_api = 21

android.archs = arm64-v8a, armeabi-v7a

android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
