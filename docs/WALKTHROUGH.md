# Walkthrough brief

Input for the exploratory walkthrough skill: an agent with a headless browser uses the live site
as a visitor would and reports what gets in the way. Keep the by-design list current, or the
walk re-reports the same intentional behaviour every time.

## Site

- Live: https://buergerwecker.de (production, real subscribers, real city portals behind it).
- Languages: German by default, `?lang=en` on any page for English. No redirect by browser locale.
- What it is: it watches German city appointment portals (Bürgeramt, Kfz, Führerschein, Ausländerbehörde,
  Standesamt and more, 30 cities) and tells a subscriber by email, or by push in the iPhone/Android app,
  when a matching slot opens. The subscriber books on the city's own page. No account, no login, no cookies.
- Pages: city picker `/`; one sign-up page per city or office at `/<slug>` (for example `/muenster-kfz`,
  `/leipzig`, `/leipzig-abh`, `/dresden`, `/bonn`); `/datenschutz`, `/impressum`, `/kontakt`. The FAQ sits at the
  bottom of `/` and of every city page. An unknown slug answers 404 with the picker.
- Wide cities have several offices (Münster: Bürgeramt, Kfz, Einbürgerung, Standesamt, Gewerbe …): the picker
  lists them under the city and each opens its own page. A city page links its sibling offices and the other cities.

## Personas

1. **Kfz in Münster, phone.** Came from the city's own portal, which has no free slot. He needs a Kfz
   appointment (Zulassung or Führerschein-Umtausch) in Münster, reads German, and has never heard of the site.
   He opens the Münster Kfz page, picks the service, picks the weekdays and times he can make, and fills in the
   form up to the Anmelden button. Does he understand what happens after the click (mail, confirm, end date)?
2. **Foreign student, phone, English.** New in Leipzig, browser in English, needs the Ausländerbehörde
   (residence-document pickup). He wants to know whether the site does this for his city, what it can and cannot do
   (booking? the Termin-Code?), and whether there is an app.
3. **City comparer, desktop.** Moving house within a year, undecided between Dresden, Leipzig and Bonn. He browses
   the picker and a few city pages, reads the FAQ, looks at privacy and the Impressum, and wants to know who runs
   this and what it costs before he gives an email address.

## Key flows

- City picker `/`: pick a city, pick an office where a city has several, the "Wähle deine Stadt (N)" count.
- A city page: heading, sibling offices, "Weitere Städte & Ämter", Anliegen (service) dropdown, Alle Standorte /
  Bestimmte Standorte (offices that do not offer the chosen service disappear), Zeitfenster, "Nur Termine innerhalb der
  nächsten …", Wochentage, the notices above the button, up to but not including "Anmelden".
- Language switch `?lang=en` on `/`, a city page, the FAQ, legal pages and `/kontakt`; the switch keeps the page.
- FAQ (all questions) and the app hints: the iPhone App Store link and the two Android steps (join the test group,
  then sign up as a tester). Read them and open the links one hop, nothing further.
- Legal pages: `/datenschutz`, `/impressum`, and `/kontakt` (open and read only).
- Unknown city (`/gibtsnicht`): 404 with the picker and a message.
- `/go/<slug>` is the booking link from mails: see out of bounds.

App-only, skip on a web walk: push registration, the in-app alerts list and slot overview, widgets, the app's own
sign-up without an email address, anything that needs the installed iOS/Android app.

## By design — do not report

- **It never books.** It only notifies; the visitor books on the city's page. The site says so in the intro and FAQ
  ("Bucht das Tool Termine? Nein.").
- **Double opt-in.** After sign-up nothing is sent until the mail link is clicked; the post-sign-up banner says so.
  The confirmation mail is a plain text with one link. (Walkers cannot see it: do not sign up to check.)
- **Mail is capped.** At most N mails per 24 hours per subscriber (N is on the page: FAQ "Wie viele Mails bekomme
  ich?"); later slots go into the next mail. Under load the page shows "E-Mails sind gerade knapp: …" with a lower
  cap, and may say the confirmation mail can be delayed "eventuell bis morgen". Both notices come and go with the
  day's mail volume; the app is offered as the way round them.
- **A subscription ends by itself.** The FAQ states the term in days ("Wie lange läuft meine Anmeldung?"), the exact end date
  is shown after confirming and in the management mail, and a "Suchst du noch?" mail one click away extends it. Sensitive services
  get a shorter term, said in their consent text.
- **Some services are not on the site, or are website-only.** Services whose bare selection reveals health, religion
  or sexual orientation (disability ID and parking, church exit, partnership documents, medical rehab and the like)
  are left out on purpose, so a city's dropdown can look shorter than the city's own portal. Where a city has agreed
  (Münster Gesundheitsamt STI-Beratung, the SBGG declaration), the service is offered with a separate Art. 9
  consent box that appears only while that service is selected; its mails never name the service or the office.
  Three offices are website-only and not in the app: the Leipzig Ausländerbehörde (`/leipzig-abh`), the Münster Einbürgerung (`/muenster-einbuergerung`) and the Münster Gesundheitsamt (`/muenster-gesundheitsamt`).
- **Offices without the chosen service are hidden.** Changing Anliegen hides offices that do not offer it and
  unchecks them. A service the site has no map for shows every office.
- **Dedicated Kfz, Führerschein and Ausländerbehörde pages exist only for a few cities** (Kfz: Münster, Bochum;
  Führerschein: Bochum, Stuttgart; Ausländerbehörde: Leipzig). Some Bürgeramt dropdowns also list single Kfz or
  Führerschein services, which is not a bug.
- **Dresden (and every other TEVIS city, such as Münster and Braunschweig) reports only the earliest slot per
  office; Leipzig lists all slots.** This shows in the mails, not on the web pages, and a later slot on an
  already-reported day stays quiet. Not visible on a web walk.
- **A queue notice** ("Für X warten gerade N Personen auf einen Termin. Rechne also mit Wartezeit.") appears
  only on busy city pages. Its number counts mail and app subscribers of that city.
- **Android is a closed test with two steps** (Google Group, then Play opt-in); production access is not open yet.
  iPhone has a normal App Store link.
- **Text-only mails**, no HTML, no button, no tracking pixel.
- **No accounts, no login, no cookies, no third-party scripts.** Subscriptions are managed through a link in
  the mail, not through the site.
- **Not affiliated with any city.** Every page carries the disclaimer. The footer carries Impressum, Datenschutz, the iPhone and Android app links, "Kaffee spendieren" (Ko-fi) and "Quellcode".
- **`/?city=x` redirects (301) to `/x`.** An unknown city gives a 404 page, on purpose.
- **Impressum and Datenschutz** exist in German and English.
  `/kontakt` is the contact form required by § 5 DDG and serves several sites; its "Um welche Seite geht es?" menu lists them.
- Outbound links send no Referer (`no-referrer`).

## Out of bounds

The site is production. The walk must never create anything or put load on a city portal.

- **No POST of any kind.** Never submit the sign-up form, so never press "Anmelden"/"Subscribe": it creates a real
  subscription, sends real mail and can add polling load on a city portal. Fill the fields if you like and stop
  at the last screen before the button; note what you would have done.
- **Never submit `/kontakt`.** Open and read it, do not fill the email or message field.
- **Never open token links:** `/confirm/…`, `/manage/…`, `/unsubscribe/…`, `/renew/…`, `/go/sub/…`, or any check-in link,
  even if one is shown or guessable.
- **No `/admin`**, no `/webhooks/…`, no `/api/v1` calls (the app's backend; writes are real).
- **No load testing and no rapid reloads.** Out of courtesy to a small production server, keep to a page every few
  seconds and do not crawl all 30 cities. (The per-IP rate limiter only covers sign-up and contact POSTs, which you never send.)
- **City portals:** `/go/<slug>` redirects to the city's booking page. Open one at most per city and go no further than
  its first page; select nothing, submit nothing, book nothing. Prefer not to open it at all and note the target.
- **Store pages:** open the App Store link, not the install. The Google Group and Play opt-in links: read the
  first screen, do not join, do not sign in, do not opt in.
- **Ko-fi link:** do not open a payment.
- Never use a real name or email address; `@example.com` only, and only to fill a field you will not submit.
