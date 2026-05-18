# Deployment

## Prerequisites

- Raspberry Pi running Docker + Docker Compose.
- Domain `termine.jakubwaller.eu` (or replacement) pointing to the Pi's
  public IP. Ports 80 and 443 forwarded to the Pi.
- USB HDD mounted at `/mnt/backup` (auto-mount via `/etc/fstab`).
- Mailjet and Resend accounts with verified sender domain.
- SPF / DKIM / DMARC records configured on `jakubwaller.eu` before any send.

## First deploy

1. Clone the repo to the Pi.
2. Copy `.env.example` to `.env` and fill in real secrets:
   - 32-byte `TOKEN_SECRET_PRIMARY` and `ADMIN_TOKEN` (e.g., `openssl rand -hex 32`).
   - Mailjet and Resend API keys.
3. Verify the USB HDD is mounted at `/mnt/backup`.
4. `docker compose up -d`.
5. Watch logs: `docker compose logs -f`.
6. Verify healthz: `curl https://termine.jakubwaller.eu/healthz`.

## Token-secret rotation

1. Set `TOKEN_SECRET_PREVIOUS=$TOKEN_SECRET_PRIMARY` in `.env`.
2. Generate a new secret: `openssl rand -hex 32` → `TOKEN_SECRET_PRIMARY`.
3. `docker compose restart web poller`.
4. Existing tokens remain valid; next rotation invalidates them.

## SMART monitoring (host-side systemd timer)

Install `smartmontools`. Add `/etc/systemd/system/termine-smart.service`:

```
[Unit]
Description=Termine-Notifier SMART check

[Service]
Type=oneshot
ExecStart=/path/to/termine-notifier/scripts/smartcheck.sh /dev/sda
```

And `/etc/systemd/system/termine-smart.timer`:

```
[Unit]
Description=Weekly SMART check

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
```

`systemctl enable --now termine-smart.timer`.

## Off-Pi backup (secondary)

From your laptop:

```
rsync -av jakub@pi:/mnt/backup/ ~/termine-backups/
```

Cron weekly.

## IP-block runbook

If `terminvereinbarung.leipzig.de` starts returning 403 for the Pi's IP:

1. Stop the poller: `docker compose stop poller`.
2. Email the city: `verwaltung@leipzig.de`. Subject: "Anfrage zu
   Terminvereinbarung-Notifier". Explain: free notification service, no
   booking, polling rate ≤ 10 req/min, GDPR-compliant, open source at
   `github.com/jakubwaller/termine-notifier`. Ask if there is a way to
   continue operation that the city would accept.
3. Do NOT attempt to rotate IPs or use proxies — this is ethically
   worse than the polling itself and undermines the legal posture.
