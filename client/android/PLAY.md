# Google Play

The Android app is the same Capacitor shell as the iPhone one (`client/www/`
inside a WebView). It builds on GitHub's runners, never on a laptop: the `play`
task of `.github/workflows/app-build.yml`.

## Keys and secrets

| What | Where |
|---|---|
| Upload key (PKCS12, alias `upload`) | made once with openssl (below), kept outside the repository; no backup needed, a lost one is a reset request (below) |
| `ANDROID_UPLOAD_KEYSTORE` | repo secret: `upload.p12`, base64 |
| `ANDROID_UPLOAD_KEYSTORE_PASSWORD` | repo secret: the password (store and key share it) |
| `PLAY_SERVICE_ACCOUNT_JSON` | repo secret: the service account's JSON key (below). Optional — without it the bundle is only a run artifact |
| `FCM_GOOGLE_SERVICES_JSON` | repo secret: Firebase's `google-services.json` for `de.buergerwecker.app`. Without it the app builds but receives no pushes (`PushGatePlugin`) |
| `PLAY_TRACK`, `PLAY_RELEASE_STATUS` | repo *variables*, default `alpha` (closed testing) and `draft` |

It is the **upload** key. Play App Signing keeps the key phones actually verify,
so a lost or leaked upload key is a reset request in the Console (Setup → App
signing → Request upload key reset), not the end of the app.

Made with openssl, so no JDK is needed to make or renew it:

```bash
mkdir -p ~/gitlab/buergerwecker-android-upload-key && cd ~/gitlab/buergerwecker-android-upload-key
openssl rand -base64 24 > password.txt
openssl req -x509 -newkey rsa:4096 -sha256 -days 10950 -nodes \
  -keyout upload.key -out upload.crt -subj "/CN=Buergerwecker upload key/O=Buergerwecker/C=DE"
openssl pkcs12 -export -name upload -inkey upload.key -in upload.crt \
  -out upload.p12 -passout file:password.txt && rm upload.key
base64 -i upload.p12 | gh secret set ANDROID_UPLOAD_KEYSTORE
gh secret set ANDROID_UPLOAD_KEYSTORE_PASSWORD < password.txt
```

## Push (Firebase Cloud Messaging)

The push plugin delivers through FCM, which needs a Firebase project:

1. console.firebase.google.com → Add project (Analytics off) → Add app → Android,
   package name `de.buergerwecker.app`.
2. Download `google-services.json` and put it in the secret:
   `gh secret set FCM_GOOGLE_SERVICES_JSON < google-services.json`.
   CI writes it to `client/android/app/google-services.json` before `cap sync`; the
   file is git-ignored. For a local build, copy it there by hand.
3. Project settings → Service accounts → Generate new private key: that JSON is the
   server's `FCM_SERVICE_ACCOUNT_JSON(_FILE)` (`docs/DEPLOY.md`, "Push delivery (the
   app)"), not a GitHub secret.

A build without the file compiles and runs; `PushGatePlugin` tells the page that
pushes are unavailable, so it shows a hint instead of crashing in
`PushNotifications.register()`.

## Play Integrity

The server only creates an Android device, or takes a new push token for one, with
a Play Integrity verdict showing the genuine app on a genuine device
(`IntegrityPlugin`, `app/integrity.py`); without it a headless FCM receiver could
mint verified devices. One-time setup:

1. Play Console → the app → **Protected with Play → Play Integrity API → Get
   started → Link cloud project**: pick the Google Cloud project behind the Firebase
   project (its number is the sender ID in `google-services.json`, which the plugin
   reads, so the two cannot drift apart). The Responses table below it must show
   Application integrity and Device integrity **On**, the two verdicts the server
   requires.
2. In that Google Cloud project, the **Play Integrity API** must be enabled; the link
   in step 1 enabled it by itself (2026-10-09).
3. The server decodes verdicts with a service account, and that account must
   belong to the Google Cloud project linked in step 1. A service account from
   any other project is refused by Google with 403 on every decode, which the
   server answers as 503 to every Android registration. Use the FCM service
   account of the linked Firebase project (`FCM_SERVICE_ACCOUNT_JSON(_FILE)`);
   it is the default, and `PLAY_INTEGRITY_SERVICE_ACCOUNT_JSON(_FILE)` should stay
   unset unless it is another account of the same project (`docs/DEPLOY.md`,
   "Push delivery (the app)").

Only builds installed from Google Play pass, closed testing included. A sideloaded
or debug build cannot register against a production server (it gets
`integrity_failed`, "must be installed from Google Play and run on a certified Android device"); to try one, point it at a
dev server running with `PLAY_INTEGRITY_REQUIRED=0`. The Play Console **data safety**
form must mention it: the app sends an integrity token to our server at
registration, and Google Play processes the request.

## First release, once

1. Play Console → **Create app**: name `Bürgerwecker`, default language German, App, Free.
2. Actions → App build → Run workflow → task **play**. The run's artifact
   `buergerwecker-aab-<n>` is the signed bundle. This repository is public, so anyone signed
   in to GitHub can download that artifact, `google-services.json` included, for its 30 days.
   The gate for that is "Before the public release" in `client/README.md`.
3. Console → Testing → **Closed testing** → create track → Create release →
   accept Play App Signing (Google-generated key) → drag the `.aab` in.
4. Testers: a Google Group or an email list, and the opt-in link to them.
   New personal accounts need **12 testers opted in for 14 days in a row**
   before *Apply for production* unlocks.
5. For later builds without the web page: Google Cloud → a project → enable the
   *Google Play Android Developer API* → a service account → a JSON key; Play
   Console → Users and permissions → invite the service account's email, app
   `Bürgerwecker`, permissions *Release to testing tracks*. Put the JSON in
   `PLAY_SERVICE_ACCOUNT_JSON`.

From then on task **play** uploads by itself (`android/play.mjs`). While the app
has never been published, Play only accepts `draft` releases, so each one waits
for a person's *Roll out* in the Console; once the app is live, set
`PLAY_RELEASE_STATUS` to `completed`.

## Version code

`github.run_number`, the same as the iOS build number: Play refuses a code it
has seen, and the run number only goes up. `versionName` in `app/build.gradle`
follows `MARKETING_VERSION` in the Xcode project; bump both in the same PR.

## What differs from the iPhone app

- **The back key** pops the current tab's stack and only then sends the app to
  the background (`backButton` in `www/app.js`).
- **Notification channel** `slots`, created by the page at start (`www/push.js`)
  and named as FCM's default channel in the manifest; the server sends
  `channel_id: "slots"`. The status-bar icon is `res/drawable/ic_stat_notify.xml`.
- **Settings button** for notifications: `PushGatePlugin.openSettings`, since
  Android has no `app-settings:` URL.
- `allowBackup="false"`: the device credentials are not restored onto another
  phone, where they would not match its push token.
