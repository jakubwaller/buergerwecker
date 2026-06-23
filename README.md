# Termine-Notifier

Free email notifications for free Bürgerbüro appointments in German cities.

**v1 covers Leipzig.** Support for more cities may follow.

## What this does

You enter your email, pick the appointment type (e.g. Wohnsitzanmeldung) and
which Bürgerbüros work for you, and receive an email the moment a matching
slot is available on `terminvereinbarung.leipzig.de`. You then book it
yourself on the official city website. We never book on your behalf.

## What this explicitly does NOT do

- **No automated booking.** The project will not accept pull requests that
  add booking functionality. Forks that add booking will remain forks — not
  merged upstream, not endorsed.
- **No account, no login, no tracking, no cookies, no third-party JS.**
- **No data resale, no advertising, no paid features.**

## How it works

A Raspberry Pi in Germany runs a small Docker Compose project: a Caddy
reverse proxy, a Flask web app for subscriptions, a Python poller that
checks the city's booking site once a minute, and a backup container that
snapshots the SQLite database to an external HDD.

## Not affiliated with Stadt Leipzig

This is an independent service. We only inform about available appointments.
The city of Leipzig has no involvement.

## License

AGPLv3 — see `LICENSE`.

## Support

If this helped you, you can buy me a coffee: <https://ko-fi.com/jakubwaller>
