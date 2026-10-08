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
gets an id and a secret, kept in the Keychain (iOS) or encrypted under an AndroidKeyStore key
(Android) and in no backup (see "The device credential" below); every authenticated call sends
`Authorization: Bearer <id>.<secret>`. "Delete my data" is one `DELETE /device`.

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
   there is no snapshot; "Watch this service" and "Book on the city's site". The slots come
   only to a device with a live alert in that city (`GET /cities/<slug>/slots` takes the
   device credential and answers `403 not_subscribed` otherwise), so without one the screen
   says to set up an alert for the city; a special-category service appears only to a device
   that watches it itself, and its card claims nothing when it is absent. Opened from an alert
   or a notification, the alert's service comes first. The snapshot is read through a one-minute
   cache (`citySlots` in `state.js`) shared with My alerts, since the route allows a device about
   60 reads an hour together with the widget; a push for the city drops that city's copy.
5. **Subscribe / edit form**: service (with the Art. 9 consent box for a `sensitive`
   service), offices filtered by the service, weekdays, time window, how far ahead.
6. **My alerts**: each with its filter, "runs until", the soonest slots that alert would notify
   about right now (the server's `app/filters.py` rules over the city's snapshot, `filters.js`)
   as a row into the city overview, "Book on the city's site" when there is one, "Keep looking"
   on expired ones, edit and stop. Loaded on launch and on resume, never on a timer.
7. **Notifications**: the slots and check-in notifications of the last 30 days (at most 50),
   newest first, an unread count on the tab, "Clear list". Kept on the phone only, in
   Preferences (`inbox.js`); "Delete my data" clears it. In: a push received in the foreground,
   a tapped one, and on iOS what still sits in the notification centre at launch and resume.
   Android hands that last call the system's copy without the push's data, so there it covers
   foreground and tapped pushes only; a tap there carries no text, and the list words the entry
   itself. Tapping an entry opens the city overview at that alert's service, never the booking
   page: the slots it named may be gone by then.
8. **Home-screen widget** (iOS small + medium, Android resizable): the earliest free slot in the
   cities of the device's active alerts, special-category (Art. 9) ones left out, "as of" the
   server's last poll. It only shows; a tap opens the app (see "The widget" below).
9. **Settings**: language (German/English, from the device language at first), delete my data,
   privacy / imprint / contact on buergerwecker.de, the version.

Notification taps: `slots` opens the booking URL in the system browser and the city's overview
in the app (at the alert's service); `checkin` opens My alerts with "Yes, keep looking" / "No, I've got one" in front;
`verify` sends the code. In the foreground a small in-app banner does the same on tap. A push's
`url` is opened only when its origin is exactly `https://buergerwecker.de` (`siteUrl` in
`api.js`); anything else is ignored. While the server's `APP_API_ENABLED` gate is closed (every
route `404 not_available`) the app shows one "not released yet" screen.

Errors: every API error body has `error` (a key) and usually `message`, a sentence in the
device's language; the app shows `message` whenever there is one (a waitlist full for this city
or for this device, too many new devices from one place in a day, a body too large) and its own
fallback per key otherwise (`errorText` in `api.js`). The one exception is `rate_limited`: the
server words every 429 with the website's "too many sign-ups" sentence, so the app always shows
its own neutral text there. Every request with a body sends
`Content-Type: application/json`; the server refuses a body that is not JSON.

## Layout

```
client/
  package.json          Capacitor and its plugins: push-notifications, preferences, browser, app
  capacitor.config.json appId de.buergerwecker.app, webDir www; CapacitorHttp off (below)
  check-www.js          `npm run build`: www/ IS the source, so the build only checks that every
                        import and every file index.html loads exists
  www/                  the app: index.html, app.js (start-up, frame, notification taps),
                        api.js (the one network module), push.js (registration, verification),
                        state.js (navigation, subscription list, catalog cache), i18n.js (every
                        string in de and en), format.js, store.js (Preferences, and the device
                        credential through SecureStore), native.js (the Capacitor seam), ui.js,
                        screens/*.js, style.css
  test/                 node --test: i18n key sets, API error mapping, formatting, push rules,
                        the credential's move out of Preferences, the native contracts
  plugins.test.mjs      Package.swift's plugin list held to package.json
  assets/               icon and splash sources (the site's alarm-clock glyph on #2563eb); every
                        size under ios/ and android/ comes from
                        `npx @capacitor/assets generate --ios --android`; afterwards rename
                        the 1024 px icon it writes into AppIcon.appiconset to AppIcon-1024.png (and in Contents.json), or
                        tests/test_no_real_pii.py reads the name as an email address
  ios/App/              the Xcode project (SPM, no CocoaPods), iOS 18
    App/                AppDelegate (hands the APNs token to the push plugin), SceneDelegate,
                        MainViewController (registers WidgetBridgePlugin and SecureStorePlugin),
                        Info.plist, App.entitlements (aps-environment, App Group, keychain
                        group), PrivacyInfo.xcprivacy
    BuergerweckerWidget/ the WidgetKit extension (SwiftUI): the widget, its Info.plist,
                        entitlements (App Group, keychain group), privacy manifest,
                        German/English gallery texts
  ios/add-widget-target.rb  registers the extension target (and the app's three Swift files)
                        with the Xcode project; idempotent, for after `npx cap add ios`
                        regenerated it
  ios/asc.mjs           App Store Connect API for the runner: bundle id, certificate, profile,
                        TestFlight "What to Test" text and beta group distribution
  ios/distribution.csr  the request the distribution certificate is signed from (no secret in it)
  ios/testflight/       what-to-test.<locale>.txt, one per TestFlight locale
  android/              the Android project; MainActivity registers PushGatePlugin (is push
                        available in this build, and the notification settings button),
                        WidgetBridgePlugin and SecureStorePlugin (SecureStore.java: the
                        credential under an AndroidKeyStore key); EarliestSlotWidget is the
                        home-screen widget; res/xml/data_extraction_rules.xml and
                        backup_rules.xml keep the credential out of backup and transfer
  android/play.mjs      Google Play Developer API: upload a signed bundle to a track
  android/PLAY.md       keys, Firebase, the first Play release
```

Bundle id and applicationId `de.buergerwecker.app` on both platforms — the server's
`APNS_TOPIC` is this bundle id. URL scheme `buergerwecker://`.

**CapacitorHttp is off**: the page's `fetch` is the WebView's own. The page's origin is
`capacitor://localhost` (iOS) or `https://localhost` (Android), so every API call is
cross-origin, and it works because the server answers CORS for exactly those two origins on
every response under `/api/v1`, routing errors and preflights included (`CORS_ORIGINS` and
`register_cors` in `app/api.py`). Two rules follow. Request headers stay within `Accept`,
`Authorization` and `Content-Type` (the server's preflight allows no others; a test holds
`api.js` to it). And if `server.iosScheme` or `androidScheme` is ever set in
`capacitor.config.json`, the origin changes and `CORS_ORIGINS` must change with it, or every call
fails. The page's CSP (`connect-src`) names `https://buergerwecker.de` only. The native widgets
fetch in Swift and Java, outside the WebView, and need no CORS.

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
Store Connect API for it. The repository is public, so anyone signed in to GitHub can download a
run's artifacts; the Mac jobs run only when `client/` changes.

Once per Apple account (task or label `apple-setup`, safe to repeat):

1. registers `de.buergerwecker.app` (Push Notifications, App Groups) and its widget extension
   `de.buergerwecker.app.widget` (App Groups);
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

The Release configuration is signed manually (`Apple Distribution`, profiles
`Buergerwecker CI de.buergerwecker.app` and `Buergerwecker CI de.buergerwecker.app.widget`, made
fresh by every build); Debug stays automatic, so
with Xcode on a Mac: select the team and run. `App.entitlements` says `aps-environment
development`; the App Store export re-signs it as `production`, which is why the server stays at
`APNS_SANDBOX=0` from the first TestFlight build on.

If `ios/` is ever regenerated (`npx cap add ios --packagemanager SPM`), re-apply by hand what
the template lacks — `git diff` on those files shows it: the deployment target 18.0 and the
Release signing settings in `project.pbxproj`, the widget target (`ruby ios/add-widget-target.rb`
puts it and the two app Swift files back; `MainViewController` must also be the class in
`Main.storyboard` and in `SceneDelegate`), `App.entitlements` and `PrivacyInfo.xcprivacy`
in the project, the URL scheme, `CFBundleLocalizations` and `ITSAppUsesNonExemptEncryption` in
`Info.plist`, and the two push callbacks in `AppDelegate.swift`.

### The widget

For App Review 4.2 / 4.2.2 the app's answer to "a repackaged website" is what a website cannot be:
push, no account, and a widget. The widget only shows; it never books and a tap opens the app.

- **Data.** It fetches `GET /api/v1/cities/<slug>/slots` itself, with the device credential
  (`Authorization: Bearer <id>.<secret>`) it reads natively from the secure store (below; the page
  never hands it over), and shows the earliest slot that would also trigger an alert: each alert's
  service plus its offices, weekdays, time window and days ahead, matched like the server's
  `app/filters.py` (inclusive time bounds, ISO weekdays, office ids, Berlin's today). The snapshot
  keeps only the 100 soonest slots per service, so a match further out than that is not seen.
  Nothing matching: "no matching slot right now" with the as-of time. Nothing new leaves the phone:
  the same host, the same route and the same credential the app's city overview uses.
- **What it shows when the answer is not a 200.** `401`, `410` or a `403` (the credential is
  refused, the device unverified or retired), or no credential at all (the app not opened since
  this version moved it, "Delete my data"): only "open the app", and the cached answers are
  deleted, so nothing stale can later pass for live; the next answer that proves the credential
  works lifts it, and so does a credential with an empty list (nothing to fetch, so "set up an
  alert" instead). `403 not_subscribed` for one city: that city drops out until the app sends a new
  list. `429` (the route's per-device budget, about 60 an hour shared with the app), `404` while
  `APP_API_ENABLED` is off, or no network: the last answer with its "as of" time; with none, "no
  data yet".
- **Which cities.** The page decides (`www/widget.js`): the cities of the *active* alerts, at most
  five, with each alert's filter, the language, and the strings and weekday/month words from
  `i18n.js` (native code cannot read it). A special-category alert (`consent_special`, Art. 9) is
  left out entirely, and so is a city only such alerts watch: a home-screen widget shows city,
  time and office to anyone who looks at the phone, which the push for such an alert withholds
  (`app/push.py`). It writes that through the local `WidgetBridge` plugin (`setConfig` / `clear`)
  after the alert list loads or changes, after a language change, and on "Delete my data", and
  skips a write that would change nothing (each one costs a fetch per city) unless the device was
  re-registered or its verification changed. No alerts: the widget asks the person to open the
  app. The list carries `v` (`CONFIG_VERSION`, 3 since special-category alerts were taken out);
  both widgets treat a lower or missing `v` as no list, so whatever an older app version left
  behind never shows, and their cache moved to a new key (`widget_cache_v3`, the old one is
  deleted) for the same reason.
- **iOS.** `BuergerweckerWidget` (bundle id `de.buergerwecker.app.widget`), SwiftUI, small and
  medium; timeline refresh about every 20 minutes (WidgetKit decides). The plugin
  (`App/WidgetBridgePlugin.swift`) writes the JSON into the App Group `group.de.buergerwecker.app`'s
  UserDefaults and calls `reloadAllTimelines()`; the extension keeps its last answers there too, and
  reads the credential from the keychain group it shares with the app. Both privacy manifests
  declare reason `1C8F.1` (the App Group's defaults) and the install's identifier sent to
  buergerwecker.de for app functionality.
- **Android.** `EarliestSlotWidget`, an `AppWidgetProvider` with a RemoteViews layout (one to three
  rows by height, light and dark), `updatePeriodMillis` 30 minutes (the platform floor; no
  WorkManager dependency) plus an update broadcast from the plugins whenever the list or the
  credential changes. The fetch runs inside that broadcast (`goAsync`), cities in parallel, 8 s
  timeouts. Data and cache live in the SharedPreferences file `buergerwecker_widget`; the
  credential comes from `SecureStore`.

`test/widget.test.mjs` holds the page's side and the native contracts (plugin methods on both
platforms, the App Group and keychain group names, the Keychain item's attributes, the storage
keys, the backup rules, every string key native code reads) together.

### The device credential

The id and secret the server hands out at registration are the device's whole identity: whoever
holds them can read and delete its alerts, special-category ones included. So they are kept out of
Capacitor's Preferences, which are UserDefaults on iOS and land in every iCloud and Finder backup.

- **iOS.** `App/SecureStorePlugin.swift` (`SecureStore` to the page) keeps them as one Keychain
  item (generic password, service `de.buergerwecker.device`, account `credential`, the JSON
  `{"id", "secret"}`), `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`, not synchronisable:
  never in iCloud Keychain, never restored onto another phone, readable in the background after
  the first unlock (the widget refreshes with the screen locked). It sits in the keychain access
  group **`$(AppIdentifierPrefix)de.buergerwecker.shared`**, which both `App.entitlements` and
  `BuergerweckerWidget.entitlements` list under `keychain-access-groups`; the extension reads it
  from there (`Credential` in `BuergerweckerWidget.swift`). Every App Store profile carries
  `keychain-access-groups <team>.*`, so the group needs no capability on the App IDs and no step on
  developer.apple.com; `ios/asc.mjs profiles` stops early if a profile ever lacks the wildcard.
- **Android.** `SecureStore.java` encrypts the same JSON with an AES-256-GCM key generated in the
  `AndroidKeyStore` (it never leaves it) and keeps the ciphertext in the private preference file
  `buergerwecker_secure`. `allowBackup="false"` was already set; on Android 12+ that no longer
  stops a device-to-device transfer, so `res/xml/data_extraction_rules.xml` excludes the file from
  both cloud backup and transfer, and `backup_rules.xml` (`fullBackupContent`) does the same for
  Android 11 and older. A new phone registers as a new device.
- **The rest of the record** (`token`, `platform`, `verified`) stays in Preferences under `device`;
  `push.js` puts the two together on launch. A credential in the Keychain with no record beside it
  is what a reinstall finds on iOS (Keychain items outlive the app); it is deleted and the app
  registers afresh, as a reinstall always has.
- **Moving off Preferences.** Builds before this one kept `id` and `secret` inside the `device`
  record. On the first launch of this one `store.migrateCredential` writes them to the secure store,
  reads them back, and only then takes them out of the record; if the secure store refuses, the
  record stays as it was and the next launch tries again. Until the updated app has been opened
  once, the widget finds no credential and says "open the app".

Every change to the credential reloads the widget. "Delete my data" removes it before anything else.

### TestFlight text

`ios/testflight/what-to-test.<locale>.txt` holds the tester-facing "What to Test" text per locale
(`de-DE`, `en-US`; the filename is the locale). After every `testflight` upload a second job,
`beta-text`, waits for the build to be processed and writes the texts to it; with the dispatch
input `distribute` ticked and the repository variable `ASC_BETA_GROUP` set, it also adds the
build to that beta group and submits it for Beta App Review. Task `beta-text-pull` prints what
TestFlight shows now. This is PapaMap's process unchanged; its README has the details.

### Android

`android/PLAY.md`: the upload key, Firebase, the first Play release, the `play` task.

An Android registration also carries a Play Integrity token (`IntegrityPlugin.java`, `integrity_token` in `www/push.js`); only builds installed from Google Play get a passing verdict, so a sideloaded build registers only against a server with `PLAY_INTEGRITY_REQUIRED=0` (PLAY.md, "Play Integrity").

## This version needs a new TestFlight build

The credential's move into the Keychain / AndroidKeyStore, the widget's credential and the
`not_subscribed` handling only exist in a build made from this code; `www/` is bundled into the
app, so no server deploy reaches a phone. Builds before it keep the secret in UserDefaults (in
every backup), and against the server that requires the credential on
`GET /cities/<slug>/slots` their widget gets a `401` and their city overview no slots. Order: the
server change and this one both on `main` and deployed (the API stays closed meanwhile), then a
`testflight` run from `main`, then the gate. The keychain group needs nothing on
developer.apple.com (see "The device credential").

## Before the first TestFlight build

A checklist for the parts no workflow can do.

**Apple**

- [ ] Secrets `APPLE_TEAM_ID`, `ASC_ISSUER_ID`, `ASC_KEY_ID`, `ASC_API_KEY_P8` (a team API key,
      Admin — the PapaMap values work, it is the same account).
- [ ] developer.apple.com → Identifiers → **App Groups** → +: create `group.de.buergerwecker.app`.
      No API key can do this. Then, after `apple-setup` below has registered the two App IDs,
      open each of `de.buergerwecker.app` and `de.buergerwecker.app.widget` → App Groups →
      Configure → tick the group. (`testflight` stops early, naming this, if a profile lacks it.)
- [ ] Run task `apple-setup` (registers the App IDs `de.buergerwecker.app` with Push
      Notifications and App Groups and `de.buergerwecker.app.widget` with App Groups, signs the
      certificate); turn its artifact into `IOS_DIST_P12` and
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
- [ ] Play Console app record, first bundle by hand, then `PLAY_SERVICE_ACCOUNT_JSON`. The
      widget needs nothing in the Console (no permission, no extra declaration).

**Server**

- [ ] `APP_API_ENABLED` open once the app should work against production; until then the app
      shows "not released yet".

GitHub secrets, all in one place: `APPLE_TEAM_ID`, `ASC_ISSUER_ID`, `ASC_KEY_ID`,
`ASC_API_KEY_P8`, `IOS_DIST_P12`, `IOS_DIST_P12_PASSWORD`, `ANDROID_UPLOAD_KEYSTORE`,
`ANDROID_UPLOAD_KEYSTORE_PASSWORD`, `PLAY_SERVICE_ACCOUNT_JSON`, `FCM_GOOGLE_SERVICES_JSON`.
Repository variables (optional): `ASC_BETA_GROUP`, `PLAY_TRACK`, `PLAY_RELEASE_STATUS`.

## Before the public release

Internal, not for the store texts. `APP_API_ENABLED` was opened on 2026-10-07 for TestFlight and
closed again the same day, when the review below found a hole; it was reopened that evening, once
every fix listed there was deployed.

A verified device is not a real phone on Android. `google-services.json` ships in every APK,
and a headless FCM receiver can register real tokens under the project and receive the
verification push without one. On iOS one install's push token could be presented under many
device records in aliased spellings until #110, which normalises tokens and keeps one device per
token. Every review here assumes scripted, unlimited verified Android devices.

- [x] **Security review of the app API, 2026-10-07**: three read-only reviews in parallel
      (device auth, abuse and limits, HTTP surface). Fixed in:
      - #107: sign-up filters de-duplicated, 16 kB bodies on `/api/v1`. The critical finding: an
        office list repeated thousands of times could go out to the city's booking site.
      - #110: push-token validation, a verification budget and daily ceiling per token, a token
        gate per network (IPv4, IPv6 /64 and /48), slots only for a verified device subscribed in
        that city, an Art. 9 service only to a device that watches it, JSON bodies only.
      - #109: the app's share of a city's plan cap, at most three alerts per city per device, a
        50-slot horizon, a push budget per cycle, the same tightened cap for mail and app under
        mail pressure, and one queue for both, where push waits behind mail only for an hourly
        quota wall.
      - #108: the device secret in Keychain/Keystore and out of backups, the widget with that
        credential and without Art. 9 alerts, a push-URL allowlist.
      - #111, #112: the privacy page for the app, the network identifier, Apple and Google, the
        right to object, backups.
- [x] **A final combined review** of the API, `app/planning.py` and the client after all of the
      above, **before the first `play` run**: its signed AAB, `google-services.json` inside, is
      uploaded as an Actions artifact of this public repository, and from then on anyone holding
      it can mint verified devices. The same holds for any other Android build that leaves the
      team, Play internal testing included. Check the per-network gate, the per-token budget
      and the app's share of the plan cap against a scripted fleet.
      **Done 2026-10-07** at `56eba44`: a read of `app/api.py`, `app/planning.py`, the device and
      cap queries in `app/repo.py`, the verification push, the Android and iOS client and
      `app-build.yml`, plus a fleet run through the test client at the production limits, each
      device verified the moment it registered, as a headless FCM receiver would. No new hole,
      and nothing the `play` run exposes beyond what this section already assumes. Measured:
      - The gate: one IPv4 address mints 60 verified devices an hour, one IPv6 /48 600 (its /64s
        at ten each per ten minutes). A device that verifies leaves the unverified count, so the
        sweep defers none of them; the gate is the only bound on the rate.
      - The per-token budget does not bind a fleet, which uses a new token per device. It is
        what keeps a known token from being made to buzz, and that holds.
      - The share: 60 devices in Bonn (62 services, cap 16) got 26 alerts; app-held services
        stopped at 8 (`cap // 2`), and a new service was still open to mail and closed to the
        app. A city is polled for at most `cap + cap // 2` services, whatever the fleet does.
      - The ceiling: 34 devices fill a city's 100 app places (`MAX_APP_SUBSCRIPTIONS_PER_CITY`),
        about half an hour from one IPv4 address and at once from one /48. A device holds ten
        alerts across cities, so all 38 tenants' 3,800 places take about 380 devices, some six
        hours from one address or forty minutes from one /48. After that, new app
        sign-ups there get 503 `waitlist_full`. A subscriber who renews within the term keeps
        the place, and the website is not affected.
      - The client: nothing found. CSP, push URLs on the site's own origin only, the credential
        in Keystore and Keychain (`ThisDeviceOnly`) and out of backups, the widget's fixed API
        base (the slug comes from the server; only Android percent-encodes it), no HTML sinks,
        and no handler for the `buergerwecker://` scheme.
- [ ] **Play Integrity at registration**, decided 2026-10-07 for the fleet residual above, which
      let anyone keep new app users out of every city for as long as they kept renewing. A new
      FCM device, or an FCM device's new token, needs a Play Integrity verdict: the app as Google
      Play recognises it, on a device that meets device integrity, bound to that push token. The
      server fails closed, and iOS is unchanged (an APNs token needs a real phone). It goes in
      before the first `play` run, since a build without it cannot register against a server
      that requires it.
- [ ] **Hardening, not urgent**: the `testflight` job sets the App Store Connect key in the job's
      `env`, so `npm ci`, `npx cap sync` and the archive all run with it, and it writes the key
      file for xcodebuild before the archive, which does not need it. No package in the lockfile
      has an install script today, but `cap sync` runs the Capacitor CLI and its dependencies
      all the same. Closing it takes both: the key in the env of only the steps that use it, and
      the key file written just before the export, its one user.
- [ ] **Firebase**: accept the Data Processing and Security Terms in the Firebase project, then
      add Google to the privacy page's list of processors with a DPA.

## Not in this version

No store listing texts or screenshots, no in-app booking (never), no widget configuration screen
(it follows the alerts).
