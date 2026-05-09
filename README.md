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
  <em>Self-hostable web port of <a href="https://github.com/kl3mta3/Vitriol">Vitriol</a> — file converter with Philosopher's Stone steganographic mode, plus user accounts, role-based access, and an HTTP API.</em>
</p>

---

## What this is

This repo packages Vitriol's conversion engine behind a FastAPI web UI and a Docker image. The desktop app remains in its [original repo](https://github.com/kl3mta3/Vitriol); this repo is the web/server flavor.

You get:

- The same conversion engine (text / images / audio / video / 3D models, ~60 input formats).
- The same **Philosopher's Stone** mode (cross-format byte-preserving conversions, AES-256 password protection, self-extracting `.py` / `.exe` outputs).
- A web UI styled to match the desktop app (Cinzel display font, alchemical border, drop-zone playlist).
- User accounts with five roles — Super Admin / Admin / User / Pending / Viewer.
- Per-user and per-role rate limits + daily conversion quotas.
- Email + Discord notifications.
- Google, GitHub, and generic OpenID Connect SSO (Authentik, Keycloak, Auth0, Okta, Zitadel — any OIDC-compliant IdP).
- An HTTP API with API keys for CLI and scripting.
- Per-user theme picker — six built-in themes (default purple, crimson, verdant, cobalt, parchment, obsidian).
- A single Docker image with a SQLite volume by default (Postgres swap is one env var).

## Quick start

```bash
cp .env.example .env
# Edit .env — set VITRIOL_SECRET_KEY (everything else is optional).
docker compose up --build
```

Then open `http://localhost:8000/`. The app routes everything to **/setup** until a super admin exists; fill in the form and you'll be signed in directly to the Server tab where SMTP, OAuth providers, sign-up policy, etc. live.

For automated deployments, you can pre-bake the super admin via env vars (`VITRIOL_SUPERADMIN_*` in `.env.example`); if you do, /setup self-disables and never appears.

## CLI

```bash
python -m web.cli.vitriol_cli login --url http://localhost:8000 --key vit_xxx_yyy
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

The container serves plain HTTP on `:8000`. Front it with Caddy / Traefik / Nginx for TLS, or wire the SSL cert webhook (Server tab) to drop `fullchain.pem` + `privkey.pem` into `/data/certs/` for your proxy.

## License

See [LICENSE](LICENSE).
