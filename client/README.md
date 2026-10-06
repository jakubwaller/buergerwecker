# Bürgerwecker, the store app

The iPhone and Android app for [buergerwecker.de](https://buergerwecker.de): pick a city, a
service and the offices that work for you, and get a **push notification** the moment a
matching slot appears on the city's official booking page. You book it yourself, there — the
app never books, and never will (the product boundary in the repository's `README.md`).

It is a [Capacitor 8](https://capacitorjs.com) shell around a small web app that lives only
here, in `www/`: vanilla HTML, CSS and JavaScript modules, no framework, no bundler. The page
talks to **buergerwecker.de and nothing else** — the server's JSON API (`app/api.py`, under
`/api/v1`, documented in `docs/DEPLOY.md`). It adds no request to any city's website: the slot
overview shows the server's own last poll, and "Book on the city's site" opens the server's
`/go/<slug>` redirect in the system browser.

There is no account and no address. The phone registers its push token (`POST /devices`) and
gets an id and a secret, kept in the Preferences plugin and nowhere else; every authenticated
call sends `Authorization: Bearer <id>.<secret>`. "Delete my data" is one `DELETE /device`.

## What the app does

1. **Onboarding** (first launch): two sentences, then the notification prompt. Without
   permission the app still works for browsing, with a hint and a button to the OS settings.
2. **Setup push.** After registering, the server sends a test push (`{type: "verify", code}`)
   and treats the device as real only once the code comes back (`POST /device/verify`); until
   then every authenticated route answers `403 device_unverified`, and the app shows one
   waiting screen with a Resend button (`POST /device/verify/resend`, one a minute,
   `retry_after` on a 429). `GET /device` is read on every launch: a device the server asks to
   verify again (a reinstall re-registering the same token) gets the waiting screen again, and
   so does a token rotation: `PUT /device` with a new token answers `verified: false` and a
   setup push to the new token follows. A verify push is posted whenever it arrives.
   A server without these routes answers 404 on them and reports no `verified`; the app takes
   that as "not yet" and carries on.
3. **Cities**, grouped by city like the website's switcher (one city, several offices).
4. **City overview**: per service the earliest free slot and the soonest few, "as of" the
   server's last poll in local time, "+N more"; "nobody is watching this service yet" where
   there is no snapshot; "Watch this service" and "Book on the city's site".
5. **Subscribe / edit form**: service (with the Art. 9 consent box for a `sensitive`
   service), offices filtered by the service, weekdays, time window, how far ahead.
6. **My alerts**: each with its filter, "runs until", "Keep looking" on expired ones, edit and
   stop. Loaded on launch and on resume, never on a timer.
7. **Settings**: language (German/English, from the device language at first), delete my data,
   privacy / imprint / contact on buergerwecker.de, the version.

Notification taps: `slots` opens the booking URL in the system browser and the city's overview
in the app; `checkin` opens My alerts with "Yes, keep looking" / "No, I've got one" in front;
`verify` sends the code. In the foreground a small in-app banner does the same on tap. While the
server's `APP_API_ENABLED` gate is closed (every route `404 not_available`) the app shows one
"not released yet" screen.

## Layout

```
client/
  package.json          Capacitor and its plugins: push-notifications, preferences, browser, app
  capacitor.config.json appId de.buergerwecker.app, webDir www; CapacitorHttp on (below)
  check-www.js          `npm run build`: www/ IS the source, so the build only checks that every
                        import and every file index.html loads exists
  www/                  the app: index.html, app.js (start-up, frame, notification taps),
                        api.js (the one network module), push.js (registration, verification),
                        state.js (navigation, subscription list, catalog cache), i18n.js (every
                        string in de and en), format.js, store.js (Preferences), native.js (the
                        Capacitor seam), ui.js, screens/*.js, style.css
  test/                 node --test: i18n key sets, API error mapping, formatting, push rules
  plugins.test.mjs      Package.swift's plugin list held to package.json
  assets/               icon and splash sources (the site's alarm-clock glyph on #2563eb); every
                        size under ios/ and android/ comes from
                        `npx @capacitor/assets generate --ios --android`; afterwards rename
                        the 1024 px icon it writes into AppIcon.appiconset to AppIcon-1024.png (and in Contents.json), or
                        tests/test_no_real_pii.py reads the name as an email address
  ios/App/              the Xcode project (SPM, no CocoaPods), iOS 18
    App/                AppDelegate (hands the APNs token to the push plugin), SceneDelegate,
                        Info.plist, App.entitlements (aps-environment), PrivacyInfo.xcprivacy
  ios/asc.mjs           App Store Connect API for the runner: bundle id, certificate, profile,
                        TestFlight "What to Test" text and beta group distribution
  ios/distribution.csr  the request the distribution certificate is signed from (no secret in it)
  ios/testflight/       what-to-test.<locale>.txt, one per TestFlight locale
  android/              the Android project; MainActivity registers PushGatePlugin (is push
                        available in this build, and the notification settings button)
  android/play.mjs      Google Play Developer API: upload a signed bundle to a track
  android/PLAY.md       keys, Firebase, the first Play release
```

Bundle id and applicationId `de.buergerwecker.app` on both platforms — the server's
`APNS_TOPIC` is this bundle id. URL scheme `buergerwecker://`.

**CapacitorHttp is enabled**, which routes the page's `fetch` through native HTTP. The API sends
no CORS headers and the page's origin is `capacitor://localhost` (iOS) / `https://localhost`
(Android), so a plain WebView fetch would be refused. If the server ever answers CORS for those
origins, the setting can go.
The server now answers CORS for those origins (`app/api.py`), so CapacitorHttp could be turned off.

## Build

```bash
cd client
npm ci
npm test              # TZ=Europe/Berlin node --test
npm run sync          # check www/, then npx cap sync (both platforms)
npx cap open ios      # Xcode
npx cap open android  # Android Studio
```

Needs Node 22+, Xcode 26+ (App Store Connect refuses older SDKs), and for Android a JDK 21 and
Android Studio. `npx cap sync` regenerates `ios/App/CapApp-SPM/Package.swift` from whatever
plugins it finds actually installed under `node_modules` — and that file **is** committed,
because Xcode reads it straight out of the checkout.

**Run `npm ci` before every `npx cap sync`.** A sync over a stale `node_modules` — one missing a
plugin `package.json` already lists, say after a merge that added one — silently rewrites
`Package.swift` to match what is installed and drops that plugin from it, with nothing to say
so until a Swift build fails on a missing symbol. `plugins.test.mjs` holds the two lists to
each other, so a dropped plugin fails CI instead.

Checking the page without a phone: `native.js` only needs `window.Capacitor`. A page that
defines a fake one (`isNativePlatform() → true`, Preferences and PushNotifications answering
from memory) and a stubbed `fetch` before `app.js` runs exercises every screen in a desktop
browser — that is how the first version was checked.

Android without `google-services.json` (every local and PR build until the Firebase project
exists) compiles and runs, but has no push: `PushGatePlugin` tells the page so, instead of
letting `PushNotifications.register()` crash on an uninitialised FirebaseApp.

### iOS: signed on the runner, no Mac needed

`.github/workflows/app-build.yml` runs the node tests and compiles both platforms unsigned on
every PR push that touches `client/`, and on request archives, signs and uploads the iOS app to
App Store Connect (TestFlight): *Run workflow → testflight* in the Actions tab, or the label
`testflight` on a pull request to get that branch onto a phone. `ios/asc.mjs` talks to the App
Store Connect API for it. The macOS minutes count against the plan here (private repository),
so the Mac jobs run only when `client/` changes.

Once per Apple account (task or label `apple-setup`, safe to repeat):

1. registers `de.buergerwecker.app` and switches Push Notifications on for it;
2. has Apple sign `ios/distribution.csr` into the distribution certificate. The CSR's private
   key stays on the machine that made it; key + certificate go into the secrets
   `IOS_DIST_P12` (base64) and `IOS_DIST_P12_PASSWORD`:

   ```bash
   cd ~/gitlab/buergerwecker-ios-signing   # distribution.key lives here, never in the repo
   # distribution.cer: the apple-setup run's artifact
   openssl x509 -inform DER -in distribution.cer -out distribution.pem
   openssl rand -base64 24 > p12-password.txt
   openssl pkcs12 -export -legacy -inkey distribution.key -in distribution.pem \
     -out distribution.p12 -passout file:p12-password.txt
   base64 -i distribution.p12 | gh secret set IOS_DIST_P12
   gh secret set IOS_DIST_P12_PASSWORD < p12-password.txt
   ```

   The certificate lasts a year: revoke the old one on developer.apple.com, run `apple-setup`
   again, rebuild the `.p12`. An account allows only a few distribution certificates; if
   PapaMap's is already there, `cert` says so and makes none — either reuse that one's key and
   `.p12` for this repository's secrets too, or revoke it and let both apps share the new one.

The Release configuration is signed manually (`Apple Distribution`, profile
`Buergerwecker CI de.buergerwecker.app`, made fresh by every build); Debug stays automatic, so
with Xcode on a Mac: select the team and run. `App.entitlements` says `aps-environment
development`; the App Store export re-signs it as `production`, which is why the server stays at
`APNS_SANDBOX=0` from the first TestFlight build on.

If `ios/` is ever regenerated (`npx cap add ios --packagemanager SPM`), re-apply by hand what
the template lacks — `git diff` on those files shows it: the deployment target 18.0 and the
Release signing settings in `project.pbxproj`, `App.entitlements` and `PrivacyInfo.xcprivacy`
in the project, the URL scheme, `CFBundleLocalizations` and `ITSAppUsesNonExemptEncryption` in
`Info.plist`, and the two push callbacks in `AppDelegate.swift`.

### TestFlight text

`ios/testflight/what-to-test.<locale>.txt` holds the tester-facing "What to Test" text per locale
(`de-DE`, `en-US`; the filename is the locale). After every `testflight` upload a second job,
`beta-text`, waits for the build to be processed and writes the texts to it; with the dispatch
input `distribute` ticked and the repository variable `ASC_BETA_GROUP` set, it also adds the
build to that beta group and submits it for Beta App Review. Task `beta-text-pull` prints what
TestFlight shows now. This is PapaMap's process unchanged; its README has the details.

### Android

`android/PLAY.md`: the upload key, Firebase, the first Play release, the `play` task.

## Before the first TestFlight build

A checklist for the parts no workflow can do.

**Apple**

- [ ] Secrets `APPLE_TEAM_ID`, `ASC_ISSUER_ID`, `ASC_KEY_ID`, `ASC_API_KEY_P8` (a team API key,
      Admin — the PapaMap values work, it is the same account).
- [ ] Run task `apple-setup` (registers the App ID `de.buergerwecker.app` with Push
      Notifications, signs the certificate); turn its artifact into `IOS_DIST_P12` and
      `IOS_DIST_P12_PASSWORD` as above. The private key is
      `~/gitlab/buergerwecker-ios-signing/distribution.key` on the Mac that wrote the CSR.
- [ ] App Store Connect → My Apps → + → New App: iOS, name "Bürgerwecker", primary language
      German, bundle id `de.buergerwecker.app`, SKU e.g. `buergerwecker`. The first upload fails
      without this record.
- [ ] developer.apple.com → Keys → + → Apple Push Notifications service (APNs), Production and
      Sandbox. Download the `.p8` (once only) and set it on the VPS as `APNS_KEY_P8_FILE`, with
      `APNS_TEAM_ID`, `APNS_KEY_ID`, `APNS_TOPIC=de.buergerwecker.app`, `APNS_SANDBOX=0` —
      `docs/DEPLOY.md`, "Push delivery (the app)".
- [ ] Run task `testflight` (or put the label on a PR).

**Firebase (Android push)**

- [ ] console.firebase.google.com: a project with an Android app `de.buergerwecker.app`.
- [ ] Its `google-services.json` into the secret `FCM_GOOGLE_SERVICES_JSON`.
- [ ] Project settings → Service accounts → Generate new private key: on the VPS as
      `FCM_SERVICE_ACCOUNT_JSON_FILE` (`docs/DEPLOY.md`), for a project with the Cloud
      Messaging API (v1) enabled.

**Google Play** (`android/PLAY.md`)

- [ ] Upload key → `ANDROID_UPLOAD_KEYSTORE`, `ANDROID_UPLOAD_KEYSTORE_PASSWORD`.
- [ ] Play Console app record, first bundle by hand, then `PLAY_SERVICE_ACCOUNT_JSON`.

**Server**

- [ ] `APP_API_ENABLED` open once the app should work against production; until then the app
      shows "not released yet".

GitHub secrets, all in one place: `APPLE_TEAM_ID`, `ASC_ISSUER_ID`, `ASC_KEY_ID`,
`ASC_API_KEY_P8`, `IOS_DIST_P12`, `IOS_DIST_P12_PASSWORD`, `ANDROID_UPLOAD_KEYSTORE`,
`ANDROID_UPLOAD_KEYSTORE_PASSWORD`, `PLAY_SERVICE_ACCOUNT_JSON`, `FCM_GOOGLE_SERVICES_JSON`.
Repository variables (optional): `ASC_BETA_GROUP`, `PLAY_TRACK`, `PLAY_RELEASE_STATUS`.

## Not in this version

No widget, no store listing texts or screenshots, no in-app booking (never).
