# Store screenshots

`shoot.mjs` renders the app's own `www/` in headless Chrome, under a stubbed Capacitor and a
stubbed API, at Apple's exact pixel sizes, and composes each shot with the headline and subline
from `texts.json`. PapaMap's recipe, with this app's colours.

```bash
cd client/store-screenshots
npm i --no-save playwright        # drives the installed Google Chrome, nothing else to download
node shoot.mjs /tmp/shots         # raw/ and final/ under that directory
node shoot.mjs /tmp/shots --compose-only   # texts changed, screens did not
```

Six shots per device and language: alerts, a city's services, the alert form, the city list,
notifications, onboarding. Devices: iPhone 6.9" (1320×2868) and iPad 13" (2064×2752). The script
refuses an off-spec size rather than let the store do it.

Cities and services come from the live public catalog; the alerts, slots and notifications are
staged in the script and dated from today, so the shots never show a real person's alert. The PNGs
are not committed: regenerate them, then `upload.mjs` puts `final/` on the version being prepared
in App Store Connect, one set per locale and device (run it through `asc-env.sh`, like
`ios/asc.mjs listings push`).
