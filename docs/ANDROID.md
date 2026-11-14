# Android app

The APK is a small launcher for the existing private dashboard at `https://hookups-dashboard-demo-powerbottom.zo.computer`. It opens in an Android Custom Tab so Chrome or the user's selected browser can reuse its Zo sign-in; the APK contains no passwords, API tokens, listings, collectors, or database. An internet connection and access to the Zo account are required.

The hosted dashboard currently uses a separate demo database with 10 synthetic listings. The collector and scheduler remain on the Zo computer; installing this APK does not move them onto the phone.

## Build

With Java 17, Android SDK platform 35, build-tools 35.0.0, and Gradle 8.9 installed, build locally with:

```sh
gradle --no-daemon --project-dir android :app:assembleDebug
```

The output is `android/app/build/outputs/apk/debug/app-debug.apk`. It is a debug-signed APK for sideloading, not a Play Store release.
