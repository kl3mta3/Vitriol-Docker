<div align="center">

# Vitriol-Docker

</div>

<p align="center">
<em>Visita Interiora Terrae Rectificando Invenies Occultum Lapidem</em>
</p>

<p align="center">
  <img src="resources/icons/logo-256.png" alt="Vitriol" width="160"/>
</p>

<p align="center">
<em>Visit the interior of the earth; by rectification you will find the hidden stone.</em>
</p>

---

<p align="center">
  <em>Self-hostable web port of <a href="https://github.com/kl3mta3/Vitriol">Vitriol</a> - file converter with Philosopher's Stone steganographic mode, plus user accounts, role-based access, and an HTTP API.</em>
</p>

<div align="center">
  
![License](https://img.shields.io/badge/license-ELv2-orange?style=flat-square)
![Repo size](https://img.shields.io/github/repo-size/kl3mta3/Vitriol-Docker?style=flat-square)
![Last commit](https://img.shields.io/github/last-commit/kl3mta3/Vitriol-Docker?style=flat-square)
<!-- ![Stars](https://img.shields.io/github/stars/kl3mta3/Vitriol-Docker?style=flat-square) -->

<p></P>

![Docker](https://img.shields.io/badge/Docker-ready-2496ED?style=flat-square&logo=docker&logoColor=white)
![Docker Hub pulls](https://img.shields.io/docker/pulls/kl3mta3/vitriol-docker?style=flat-square&v=2)
![Docker image size](https://img.shields.io/docker/image-size/kl3mta3/vitriol-docker/latest?style=flat-square)
![Docker version](https://img.shields.io/docker/v/kl3mta3/vitriol-docker?style=flat-square&label=docker%20version)

<p></P>

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?style=flat-square&logo=fastapi&logoColor=white)

</div>

---

## What this is

This repo packages Vitriol's conversion engine behind a FastAPI web UI and a Docker image. The desktop app remains in its [original repo](https://github.com/kl3mta3/Vitriol); this repo is the web/server flavor.

You get:

- The same conversion engine (text / images / audio / video / 3D models, ~60 input formats).
- The same **Philosopher's Stone** mode (cross-format byte-preserving conversions, AES-256 password protection, self-extracting `.py` / `.exe` outputs).
- A web UI styled to match the desktop app (Cinzel display font, alchemical border, drop-zone playlist).
- User accounts with five roles — Super Admin / Admin / User / Pending / Viewer.
- Per-user and per-role rate limits + daily conversion quotas.
- Notification fan-out — Discord, Slack, ntfy, Gotify, Telegram, Bluesky, generic webhook, bash script.
- Google, GitHub, and generic OpenID Connect SSO (Authentik, Keycloak, Auth0, Okta, Zitadel — any OIDC-compliant IdP).
- An HTTP API with API keys for CLI and scripting.
- Per-user theme picker — six built-in themes (default purple, crimson, verdant, cobalt, parchment, obsidian). Operator sets the server default; users can override in their profile.
- Operator-configurable app tips shown randomly on the conversion page — seeded with defaults, fully editable, bulk import/export.
- Branded HTML emails — logo, accent color, and border style all follow your server's configured theme automatically.
- A single Docker image with a SQLite volume by default; swap to Postgres/MySQL/MariaDB/MSSQL via the DB Providers UI or one env var.

## Quick start

Pick whichever path matches your setup. They all converge on the same first-run wizard.

### One-liner (zero-config, prebuilt image from GHCR)

```bash
curl -fsSL https://vitriol.rocks/install.sh | bash
```

Pulls `ghcr.io/kl3mta3/vitriol-docker:latest`, creates a persistent named volume, starts the container on port 3825. Re-running upgrades to the latest image without touching your data. Customize via env vars:

```bash
VITRIOL_PORT=4242 VITRIOL_VOLUME=my-vitriol curl -fsSL https://vitriol.rocks/install.sh | bash
```

### Plain `docker run`

```bash
docker run -d \
  --name vitriol \
  --restart unless-stopped \
  -p 3825:3825 \
  -v vitriol-data:/data \
  ghcr.io/kl3mta3/vitriol-docker:latest
```

### From source (compose)

```bash
git clone https://github.com/kl3mta3/Vitriol-Docker
cd Vitriol-Docker
# .env is optional — every var has a sensible default. Generate a SECRET_KEY
# only if you want to share it across replicas; otherwise the app makes one.
docker compose up -d --build
```

### Then…

Open `http://localhost:3825/`. The app routes everything to **/setup** until a super admin exists; fill in the form and you'll land on the Server tab to configure SMTP, OAuth, sign-up policy, etc.

For automated provisioning (CI / IaC), pre-bake the super admin via the optional `VITRIOL_SUPERADMIN_*` env vars in [.env.example](.env.example); if you set them, `/setup` self-disables and never appears.

## Deploy on Coolify

Coolify reads this repo's `docker-compose.yml` as-is — no fork, no edits required.

1. **Coolify → New Resource → Public (or Private) Repository.** Paste the GitHub URL, pick the branch.
2. **Build pack: Docker Compose.** Coolify auto-detects [docker-compose.yml](docker-compose.yml) at the repo root and uses [Dockerfile](Dockerfile) as the build context.
3. **Domain.** Set the public URL Coolify should route to the container (e.g. `vitriol.yourdomain.com`). Coolify provisions Let's Encrypt + Traefik labels automatically.
4. **Environment variables (all optional):**
   - `VITRIOL_SECRET_KEY` — leave blank, Coolify will auto-generate one on first boot and persist it inside the volume at `/data/.secret_key`. Set it explicitly only if you want to provision it from infrastructure-as-code or share a key across replicas.
   - `VITRIOL_DATABASE_URL` — defaults to SQLite in the volume. Set to a Postgres/Neon URL for managed-DB deploys.
   - `VITRIOL_SUPERADMIN_*` — leave unset; the in-browser `/setup` wizard runs on first visit. Set them only for fully-automated provisioning where you don't want a manual setup step.
5. **Persistent storage.** The compose file declares `vitriol-data` as a named volume mounted at `/data` — Coolify will persist it across redeploys (this holds the SQLite DB, the secret-key file, uploads, outputs, and the certs directory if you use the SSL webhook).
6. **Deploy.** First request to your domain redirects to `/setup`. Fill in the super-admin form, you're in.

After bootstrap, configure SMTP, OAuth providers, custom roles, etc. from `Server settings`. The container exposes a healthcheck that pings `/api/v1/health` so Coolify can tell when redeploys are actually serving traffic.

### Updating

In Coolify: click **Redeploy**. The image rebuilds, the volume persists, the SQLite DB and secret-key file survive untouched. The `ensure_schema()` startup hook applies any new column additions automatically — no manual migration step.

## CLI

```bash
python -m web.cli.vitriol_cli login --url http://localhost:3825 --key vit_xxx_yyy
python -m web.cli.vitriol_cli convert sample.pdf --to .png --stone --password hunter2
python -m web.cli.vitriol_cli jobs
```

API keys are issued from the Profile tab in the web UI and shown exactly once.

## Roles (BARC)

| Capability | Super Admin | Admin | User | Pending | Viewer |
|---|---|---|---|---|---|
| Run conversions | ✓ | ✓ | ✓ | — | — |
| Stone mode | ✓ | ✓ | if granted | — | — |
| Self-compile (.py / .exe) | ✓ | ✓ | if granted | — | — |
| Server settings | ✓ | — | — | — | — |
| Manage users | ✓ | ✓ (not other admins) | — | — | — |
| Create / delete admins | ✓ | — | — | — | — |
| Restart server | ✓ | ✓ | — | — | — |
| Suspend / ban | ✓ all | users + suspend admins | — | — | — |

Suspensions: 24h / 3d / 7d / 30d.

## Architecture

```
docs/        Static landing site at vitriol.rocks (GitHub Pages).
app/         Conversion engine — UNTOUCHED from the desktop app.
web/         FastAPI app, Jinja2 templates, JS, CLI, Alembic migrations.
docker/      Dockerfile + entrypoint.
resources/   Bundled stubs (selfextract_stub.exe), fonts, icons.
```

The web layer never reimplements conversion logic — it imports `app.core.router.convert_file` directly. Engine bug fixes flow through unchanged.

## Super-admin recovery

If you lose access to the super admin account, edit `/data/server_config.json` inside the volume:

```json
{
  "super_admin_recovery": {
    "username": "superadmin",
    "email": "[email protected]",
    "password": "new-password"
  }
}
```

Restart the container; the block is applied once and then wiped.

## TLS

The container serves plain HTTP on `:3825`. Front it with Caddy / Traefik / Nginx for TLS, or wire the SSL cert webhook (Server tab) to drop `fullchain.pem` + `privkey.pem` into `/data/certs/` for your proxy.

## License

See [LICENSE](LICENSE).
