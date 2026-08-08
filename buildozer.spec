[app]

# (str) Title of your application
title = Chaos Bot Mobile

# (str) Package name
package.name = chaosbot

# (str) Package domain (needed for android packaging)
package.domain = org.chaosbot

# (str) Source code where the main.py live
source.dir = .

# (list) Source files to include
source.include_exts = py,png,jpg,kv,atlas,json

# (str) Application versioning
version = 1.0

# (list) Application requirements
requirements = python3,kivy,numpy,pandas,pybit,requests,urllib3,matplotlib,certifi

# (str) Supported orientation
orientation = portrait

# (bool) Indicate if the application should be fullscreen or not
fullscreen = 0

# (list) Permissions
android.permissions = INTERNET, ACCESS_NETWORK_STATE

# (int) Target Android API
android.api = 33

# (int) Minimum API required by numpy (Исправлено: 24)
android.minapi = 24

# (str) Android NDK version
android.ndk = 25b

# (list) List of architectures to build for
android.archs = arm64-v8a

# (bool) Accept SDK license automatically
android.accept_sdk_license = True


[buildozer]

# (int) Log level (0 = error only, 1 = info, 2 = debug)
log_level = 2

# (int) Display warning if buildozer is run as root
warn_on_root = 1