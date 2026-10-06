// Every string the app shows, in German and English. test/i18n.test.mjs
// holds the two bundles to the same key set, so a string added to one
// language and forgotten in the other fails CI rather than showing a key.
// Wording follows buergerwecker.de (app/templates/ on the server side).

export const STRINGS = {
  de: {
    "app.name": "Bürgerwecker",
    "tab.cities": "Städte",
    "tab.subs": "Meine Alarme",
    "tab.settings": "Einstellungen",
    "nav.back": "Zurück",

    "onboarding.title": "Nie wieder freie Termine verpassen",
    "onboarding.p1":
      "Bürgerwecker schaut auf den offiziellen Terminseiten der Städte nach freien Terminen. Gebucht wird weiterhin von dir selbst, auf der Seite der Stadt.",
    "onboarding.p2":
      "Statt einer E-Mail bekommst du eine Mitteilung aufs Handy, sobald ein passender Termin frei wird.",
    "onboarding.allow": "Mitteilungen erlauben",
    "onboarding.browse": "Erst einmal umsehen",

    "perm.denied":
      "Ohne Mitteilungen kann die App dich nicht wecken. Umsehen geht trotzdem; für Alarme erlaube Mitteilungen in den Einstellungen.",
    "perm.prompt": "Für Alarme braucht die App Mitteilungen.",
    "perm.openSettings": "Einstellungen öffnen",
    "perm.allow": "Mitteilungen erlauben",
    "perm.unsupported": "Diese Version der App kann keine Mitteilungen empfangen. Umsehen geht trotzdem.",
    "push.registerFailed":
      "Die Anmeldung für Mitteilungen hat nicht geklappt ({reason}). Beim nächsten Start versucht es die App noch einmal.",

    "channel.name": "Freie Termine",
    "channel.description": "Eine Mitteilung, sobald ein passender Termin frei wird.",

    "cities.title": "Stadt wählen",
    "cities.search": "Stadt suchen",
    "cities.none": "Keine Stadt gefunden.",

    "city.disclaimer":
      "Diese App ist nicht offiziell mit der Stadt {city} oder ihren Behörden verbunden. Wir sind ein unabhängiger Dienst, der ausschließlich über verfügbare Termine informiert.",
    "city.asOf": "Stand: {time}",
    "city.earliest": "Frühester Termin",
    "city.more": "+{n} weitere",
    "city.noneFree": "Gerade kein freier Termin.",
    "city.unwatched": "Für dieses Anliegen hält noch niemand Ausschau.",
    "city.watch": "Dieses Anliegen beobachten",
    "city.book": "Auf der Terminseite der Stadt buchen",
    "city.bookHint": "Gebucht wird auf der offiziellen Seite der Stadt, nicht in dieser App.",
    "city.services": "Anliegen",

    "form.titleNew": "Alarm einrichten",
    "form.titleEdit": "Alarm bearbeiten",
    "form.service": "Anliegen",
    "form.allOffices": "Alle Standorte",
    "form.someOffices": "Bestimmte Standorte",
    "form.weekdays": "Wochentage",
    "form.timeWindow": "Zeitfenster",
    "form.from": "von",
    "form.to": "bis",
    "form.maxDays": "Nur Termine innerhalb der nächsten …",
    "form.noLimit": "ohne Begrenzung",
    "form.nDays": "{n} Tage",
    "form.submit": "Alarm anlegen",
    "form.save": "Speichern",
    "form.created": "Alles klar. Du bekommst eine Mitteilung, sobald ein passender Termin frei wird.",
    "form.saved": "Gespeichert.",
    "form.pickOffice": "Bitte mindestens einen Standort wählen.",
    "form.pickWeekday": "Bitte mindestens einen Wochentag wählen.",
    "form.needsConsent": "Für dieses Anliegen brauchen wir deine ausdrückliche Einwilligung.",
    "form.needsPush": "Für einen Alarm braucht die App Mitteilungen.",
    "form.notReady": "Die App ist noch nicht für Mitteilungen angemeldet. Bitte gleich noch einmal versuchen.",

    "consent.title": "Besonders geschütztes Anliegen — gesonderte Einwilligung",
    "consent.body":
      "Mit diesem Anliegen speichern wir eine Angabe, die zu den besonderen Kategorien personenbezogener Daten nach Art. 9 DSGVO gehört. Dafür brauchen wir deine ausdrückliche Einwilligung.",
    "consent.label":
      "Ich willige ausdrücklich ein, dass das gewählte Anliegen zusammen mit der Mitteilungs-Kennung dieses Geräts gespeichert und verarbeitet wird, allein um mich über freie Termine zu benachrichtigen (Art. 9 Abs. 2 lit. a DSGVO). Ich kann das jederzeit widerrufen, indem ich den Alarm beende.",
    "consent.note":
      "Die Mitteilungen nennen weder das Anliegen noch das Amt, und dieser Alarm läuft automatisch nach {days} Tagen ab. Näheres in der Datenschutzerklärung.",

    "subs.title": "Meine Alarme",
    "subs.empty": "Noch keine Alarme. Wähle eine Stadt und ein Anliegen, dann meldet sich die App, sobald etwas frei wird.",
    "subs.runsUntil": "läuft bis {date}",
    "subs.expired": "abgelaufen am {date}",
    "subs.keepLooking": "Weiter suchen",
    "subs.edit": "Bearbeiten",
    "subs.stop": "Beenden",
    "subs.confirmStop": "Diesen Alarm beenden?",
    "subs.allOffices": "alle Standorte",
    "subs.nOffices": "{n} Standorte",
    "subs.everyDay": "jeden Tag",
    "subs.anyTime": "ganztags",
    "subs.nextDays": "nächste {n} Tage",
    "subs.checkinQ": "Suchst du noch einen Termin?",
    "subs.checkinYes": "Ja, weiter suchen",
    "subs.checkinNo": "Nein, ich habe einen",
    "subs.renewed": "Weiter geht's: der Alarm läuft bis {date}.",
    "subs.stopped": "Alarm beendet.",
    "subs.unknownService": "Anliegen",

    "verify.title": "Gleich geht's los",
    "verify.body":
      "Zur Probe schickt Bürgerwecker jetzt eine Mitteilung an dieses Handy. Sobald sie da ist, kannst du Alarme anlegen. Das dauert meist nur ein paar Sekunden.",
    "verify.hint": "Nichts angekommen? Dann lass sie noch einmal schicken.",
    "verify.invalid": "Diese Probe-Mitteilung ist nicht mehr gültig. Lass dir eine neue schicken.",
    "verify.resend": "Erneut senden",
    "verify.resent": "Ist unterwegs.",
    "verify.wait": "Bitte noch {s} Sekunden warten.",
    "verify.done": "Eingerichtet. Du kannst jetzt Alarme anlegen.",

    "settings.title": "Einstellungen",
    "settings.language": "Sprache",
    "settings.delete": "Meine Daten löschen",
    "settings.deleteHint": "Löscht dieses Gerät und alle seine Alarme bei Bürgerwecker.",
    "settings.confirmDelete":
      "Damit werden dieses Gerät und alle seine Alarme bei Bürgerwecker gelöscht. Das lässt sich nicht rückgängig machen. Fortfahren?",
    "settings.deleted": "Gelöscht.",
    "settings.privacy": "Datenschutz",
    "settings.imprint": "Impressum",
    "settings.contact": "Kontakt",
    "settings.version": "Version {v}",
    "settings.noBooking": "Bürgerwecker bucht nicht für dich. Gebucht wird immer auf der offiziellen Seite der Stadt.",

    "unavailable.title": "Noch nicht freigeschaltet",
    "unavailable.body":
      "Die App ist noch nicht veröffentlicht. Bis dahin gibt es alle Städte und Alarme per E-Mail auf buergerwecker.de.",
    "unavailable.open": "buergerwecker.de öffnen",
    "unavailable.retry": "Nochmal versuchen",

    "err.generic": "Das hat nicht geklappt. Bitte später noch einmal versuchen.",
    "err.network": "Keine Verbindung zu buergerwecker.de.",
    "err.timeout": "buergerwecker.de antwortet gerade nicht.",
    "err.rate_limited": "Zu viele Anfragen von diesem Netz. Bitte in ein paar Minuten noch einmal.",
    "err.waitlist_full":
      "Für diese Stadt sind gerade alle Plätze belegt. Bitte versuch es in ein paar Tagen noch einmal.",
    "err.too_many_subscriptions": "Du hast schon {limit} aktive Alarme. Beende einen, bevor du einen neuen anlegst.",
    "err.not_found": "Diesen Alarm gibt es nicht mehr.",
    "common.retry": "Nochmal versuchen",
    "common.loading": "Lädt …",
    "common.cancel": "Abbrechen",

    "weekday.1": "Mo",
    "weekday.2": "Di",
    "weekday.3": "Mi",
    "weekday.4": "Do",
    "weekday.5": "Fr",
    "weekday.6": "Sa",
    "weekday.7": "So",
    "month.1": "Jan.",
    "month.2": "Feb.",
    "month.3": "März",
    "month.4": "Apr.",
    "month.5": "Mai",
    "month.6": "Juni",
    "month.7": "Juli",
    "month.8": "Aug.",
    "month.9": "Sept.",
    "month.10": "Okt.",
    "month.11": "Nov.",
    "month.12": "Dez.",
    "date.today": "heute",
    "date.tomorrow": "morgen",
    "date.dayMonth": "{wd}., {d}. {m}",
    "date.atTime": "{day}, {time}",
    "date.time": "{time} Uhr",

    // The home-screen widget (client/ios/App/BuergerweckerWidget, the Android
    // widget). Its native code cannot read this file, so www/widget.js hands it
    // these strings together with the weekday/month/date ones above.
    "widget.name": "Frühester Termin",
    "widget.description": "Der früheste freie Termin in deinen Städten. Gebucht wird in der App oder auf der Seite der Stadt.",
    "widget.openApp": "Lege in der App einen Alarm an, dann zeigt dieses Widget den frühesten freien Termin.",
    "widget.noMatch": "Gerade kein passender Termin",
    "widget.noSnapshot": "Noch keine Daten",
    "widget.asOf": "Stand {time}",
  },

  en: {
    "app.name": "Bürgerwecker",
    "tab.cities": "Cities",
    "tab.subs": "My alerts",
    "tab.settings": "Settings",
    "nav.back": "Back",

    "onboarding.title": "Never miss a free appointment again",
    "onboarding.p1":
      "Bürgerwecker watches the cities' official booking pages for free appointment slots. You still book yourself, on the city's own page.",
    "onboarding.p2": "Instead of an email, you get a notification on your phone the moment a matching slot opens up.",
    "onboarding.allow": "Allow notifications",
    "onboarding.browse": "Just look around for now",

    "perm.denied":
      "Without notifications the app cannot wake you. You can still look around; for alerts, allow notifications in Settings.",
    "perm.prompt": "Alerts need notifications.",
    "perm.openSettings": "Open Settings",
    "perm.allow": "Allow notifications",
    "perm.unsupported": "This version of the app cannot receive notifications. You can still look around.",
    "push.registerFailed":
      "Signing up for notifications did not work ({reason}). The app will try again the next time it starts.",

    "channel.name": "Free slots",
    "channel.description": "A notification the moment a matching slot opens up.",

    "cities.title": "Choose a city",
    "cities.search": "Search cities",
    "cities.none": "No city found.",

    "city.disclaimer":
      "This app is not officially affiliated with the City of {city} or its authorities. We are an independent service that only informs about available appointments.",
    "city.asOf": "As of {time}",
    "city.earliest": "Earliest slot",
    "city.more": "+{n} more",
    "city.noneFree": "No free slot right now.",
    "city.unwatched": "Nobody is watching this service yet.",
    "city.watch": "Watch this service",
    "city.book": "Book on the city's site",
    "city.bookHint": "Booking happens on the city's official page, not in this app.",
    "city.services": "Services",

    "form.titleNew": "New alert",
    "form.titleEdit": "Edit alert",
    "form.service": "Appointment type",
    "form.allOffices": "All locations",
    "form.someOffices": "Specific locations",
    "form.weekdays": "Days of week",
    "form.timeWindow": "Time window",
    "form.from": "from",
    "form.to": "to",
    "form.maxDays": "Only appointments within the next …",
    "form.noLimit": "no limit",
    "form.nDays": "{n} days",
    "form.submit": "Create alert",
    "form.save": "Save",
    "form.created": "All set. You get a notification the moment a matching slot opens up.",
    "form.saved": "Saved.",
    "form.pickOffice": "Please choose at least one location.",
    "form.pickWeekday": "Please choose at least one day of the week.",
    "form.needsConsent": "This appointment type needs your explicit consent.",
    "form.needsPush": "An alert needs notifications.",
    "form.notReady": "The app is not signed up for notifications yet. Please try again in a moment.",

    "consent.title": "Sensitive appointment type — separate consent",
    "consent.body":
      "Choosing this appointment type means storing information that counts as a special category of personal data under Art. 9 GDPR. That needs your explicit consent.",
    "consent.label":
      "I explicitly consent to the appointment type I selected being stored and processed together with this device's notification identifier, for the sole purpose of notifying me about free slots (Art. 9(2)(a) GDPR). I can withdraw this at any time by stopping the alert.",
    "consent.note":
      "The notifications never name the appointment type or the office, and this alert expires automatically after {days} days. Details in the privacy notice.",

    "subs.title": "My alerts",
    "subs.empty": "No alerts yet. Choose a city and a service, and the app tells you as soon as something opens up.",
    "subs.runsUntil": "runs until {date}",
    "subs.expired": "expired on {date}",
    "subs.keepLooking": "Keep looking",
    "subs.edit": "Edit",
    "subs.stop": "Stop",
    "subs.confirmStop": "Stop this alert?",
    "subs.allOffices": "all locations",
    "subs.nOffices": "{n} locations",
    "subs.everyDay": "every day",
    "subs.anyTime": "any time",
    "subs.nextDays": "next {n} days",
    "subs.checkinQ": "Are you still looking for an appointment?",
    "subs.checkinYes": "Yes, keep looking",
    "subs.checkinNo": "No, I've got one",
    "subs.renewed": "Carrying on: the alert runs until {date}.",
    "subs.stopped": "Alert stopped.",
    "subs.unknownService": "Service",

    "verify.title": "Almost there",
    "verify.body":
      "Bürgerwecker is sending a test notification to this phone. As soon as it arrives, you can set up alerts. That usually takes just a few seconds.",
    "verify.hint": "Nothing arrived? Have it sent again.",
    "verify.invalid": "That test notification is no longer valid. Have a new one sent.",
    "verify.resend": "Resend",
    "verify.resent": "On its way.",
    "verify.wait": "Please wait {s} more seconds.",
    "verify.done": "All set. You can now create alerts.",

    "settings.title": "Settings",
    "settings.language": "Language",
    "settings.delete": "Delete my data",
    "settings.deleteHint": "Deletes this device and all its alerts at Bürgerwecker.",
    "settings.confirmDelete":
      "This deletes this device and all its alerts at Bürgerwecker. It cannot be undone. Continue?",
    "settings.deleted": "Deleted.",
    "settings.privacy": "Privacy",
    "settings.imprint": "Imprint",
    "settings.contact": "Contact",
    "settings.version": "Version {v}",
    "settings.noBooking": "Bürgerwecker does not book for you. Booking always happens on the city's official page.",

    "unavailable.title": "Not released yet",
    "unavailable.body":
      "The app has not been released yet. Until then, every city and alerts by email are on buergerwecker.de.",
    "unavailable.open": "Open buergerwecker.de",
    "unavailable.retry": "Try again",

    "err.generic": "That did not work. Please try again later.",
    "err.network": "No connection to buergerwecker.de.",
    "err.timeout": "buergerwecker.de is not answering right now.",
    "err.rate_limited": "Too many requests from this network. Please try again in a few minutes.",
    "err.waitlist_full": "All places for this city are taken right now. Please try again in a few days.",
    "err.too_many_subscriptions": "You already have {limit} active alerts. Stop one before creating another.",
    "err.not_found": "This alert no longer exists.",
    "common.retry": "Try again",
    "common.loading": "Loading …",
    "common.cancel": "Cancel",

    "weekday.1": "Mon",
    "weekday.2": "Tue",
    "weekday.3": "Wed",
    "weekday.4": "Thu",
    "weekday.5": "Fri",
    "weekday.6": "Sat",
    "weekday.7": "Sun",
    "month.1": "Jan",
    "month.2": "Feb",
    "month.3": "Mar",
    "month.4": "Apr",
    "month.5": "May",
    "month.6": "Jun",
    "month.7": "Jul",
    "month.8": "Aug",
    "month.9": "Sep",
    "month.10": "Oct",
    "month.11": "Nov",
    "month.12": "Dec",
    "date.today": "today",
    "date.tomorrow": "tomorrow",
    "date.dayMonth": "{wd} {d} {m}",
    "date.atTime": "{day}, {time}",
    "date.time": "{time}",

    "widget.name": "Earliest slot",
    "widget.description": "The earliest free slot in your cities. You book in the app or on the city's own page.",
    "widget.openApp": "Set up an alert in the app and this widget shows the earliest free slot.",
    "widget.noMatch": "No matching slot right now",
    "widget.noSnapshot": "No data yet",
    "widget.asOf": "As of {time}",
  },
};

export const LANGS = ["de", "en"];

// German for a German device, English for everything else: the service
// speaks only these two, and English is the better guess for a non-German.
export function detectLang(navLang) {
  return String(navLang || "").toLowerCase().startsWith("de") ? "de" : "en";
}

let current = "de";

export function setLang(lang) {
  current = LANGS.includes(lang) ? lang : "de";
}

export function getLang() {
  return current;
}

// t("city.more", { n: 3 }) → "+3 weitere". A missing key shows the key, so a
// gap is visible rather than blank.
export function t(key, vars = {}, lang = current) {
  const s = STRINGS[lang]?.[key] ?? STRINGS.de[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
}
