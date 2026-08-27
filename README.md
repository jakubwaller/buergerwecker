# Bürgerwecker

Free email notifications for free Amt appointments in German cities.

**Live: [buergerwecker.de](https://buergerwecker.de)**

**Covered cities (29):** Augsburg, Bochum, Bonn, Bottrop, Braunschweig,
Darmstadt, Dresden, Düsseldorf, Hagen, Ingolstadt, Kaiserslautern, Kassel,
Kiel, Leipzig, Ludwigshafen, Lübeck, Mainz, Moers, Mönchengladbach,
Münster, Neuss, Nürnberg, Oberhausen, Oldenburg, Paderborn, Remscheid,
Saarbrücken, Salzgitter, Trier.

Mostly Bürgerbüro services (Wohnsitzanmeldung, Ausweise etc.). Leipzig also
has the Ausländerbehörde (residence-document pickup). The live list is on
[buergerwecker.de](https://buergerwecker.de).

## What this does

You enter your email, pick the appointment type (e.g. Wohnsitzanmeldung) and
which offices work for you, and receive an email the moment a matching
slot is available on the city's official booking site. You then book it
yourself there. We never book on your behalf.

A subscription gets at most two mails in any 24 hours
(`MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY`); slots that appear after that go into
the next mail once the window frees. The FAQ on the site says so.

For the Ausländerbehörde, booking additionally requires the personal
Termin-Code from the office's letter — this service only tells you when a
pickup slot is free.

## What this explicitly does NOT do

- **No automated booking.** The project will not accept pull requests that
  add booking functionality. Forks that add booking will remain forks — not
  merged upstream, not endorsed.
- **No account, no login, no tracking, no cookies, no third-party JS.**
- **No data resale, no advertising, no paid features.**

## How it works

A small Docker Compose project on a server in Germany: a Caddy reverse
proxy, a Flask web app for subscriptions, a Python poller that checks the
cities' booking sites every one to three minutes (depending on the city),
and a backup container that snapshots the SQLite database daily.

## Not affiliated with any city

This is an independent service. We only inform about available appointments.
None of the listed cities have any involvement.

## License

AGPLv3 — see `LICENSE`.

## Support

If this helped you, you can buy me a coffee: <https://ko-fi.com/jakubwaller>
