[app]

# (str) Title of your application
title = WiFi Phone

# (str) Package name
package.name = wifiphone

# (str) Package domain
package.domain = com.aman.wifiphone

# (str) Source code where main.py live
source.dir = .

# (list) Source files to include
source.include_exts = py,html,css,js,png,jpg,jpeg

# (str) Application version
version = 1.0

# (list) Application requirements
requirements = python3,kivy,pyjnius

# (str) Supported orientation
orientation = portrait

# (bool) Indicate if the application supports AndroidX
android.enable_androidx = True

# (int) Target Android API
android.api = 35

# (int) Minimum Android API
android.minapi = 23

# (str) Android NDK version
android.ndk = 27c

# (list) Android permissions
android.permissions = RECORD_AUDIO,MODIFY_AUDIO_SETTINGS,ACCESS_WIFI_STATE,CHANGE_WIFI_STATE,ACCESS_NETWORK_STATE,ACCESS_FINE_LOCATION,POST_NOTIFICATIONS

# (str) Presplash of the application
# presplash.filename = %(source.dir)s/data/presplash.png

# (str) Icon of the application
# icon.filename = %(source.dir)s/data/icon.png

# (str) Supported Android architectures
android.archs = arm64-v8a,armeabi-v7a

# (bool) Indicate if python-for-android should use a private storage
android.private_storage = True

# (str) Python-for-Android bootstrap to use
p4a.bootstrap = sdl2

# (str) Android entry point
android.entrypoint = org.kivy.android.PythonActivity

# (str) Android app theme
android.apptheme = @android:style/Theme.Material.Light.NoActionBar

# (str) Full name including package
# android.entrypoint = org.kivy.android.PythonActivity

# (str) Python-for-Android extra arguments
# p4a.extra_args = --color=always

[buildozer]

# (str) Log level (0 = error only, 1 = error+warning, 2 = normal, 3 = debug)
log_level = 2

# (bool) Warn if buildozer is run as root
warn_on_root = 1
