# Deploying to we-trade.me

Target server: AlmaLinux/Rocky Linux VPS at `74.208.78.218`, domain
`we-trade.me`. These steps assume you're running them yourself in a
terminal — Claude can't reach this server directly (SSH is blocked by the
sandbox's network egress rules), so this is the runbook to follow, with
`deploy/bootstrap.sh` doing most of the actual work on the server side.

## 1. Point the domain at the server

In whatever control panel manages DNS for `we-trade.me` (your domain
registrar — not necessarily IONOS's VPS panel, if you registered the
domain elsewhere), add:

| Type | Host | Value           |
|------|------|-----------------|
| A    | @    | 74.208.78.218   |
| A    | www  | 74.208.78.218   |

DNS can take anywhere from a few minutes to a few hours to propagate.
You can check with `dig we-trade.me +short` (or https://dnschecker.org) —
once it returns `74.208.78.218`, move on.

## 2. Log in to the server

```
ssh root@74.208.78.218
```

(If IONOS gave you a non-root sudo user instead, use that and prefix the
root-only commands below with `sudo`.) If this is your first login, IONOS
emailed you the initial password — you'll be prompted to change it.

**Recommended, not required:** set up an SSH key so you're not typing a
password every time:

```
ssh-keygen -t ed25519 -C "we-trade.me"   # on your Mac, if you don't have one yet
ssh-copy-id root@74.208.78.218            # copies your public key to the server
```

## 3. Copy the app code to the server

Run this **from your Mac's own Terminal** (not through Claude), from
inside the project folder:

```
rsync -avz --exclude .venv --exclude __pycache__ --exclude .git \
  --exclude data_store.db --exclude '*.pyc' \
  ~/AI-Agents/Stock-Trading/ root@74.208.78.218:/opt/stock-trading/
```

(If you've already pushed this repo to GitHub and prefer that instead:
`git clone <your-repo-url> /opt/stock-trading` on the server works just
as well — either way is fine, just pick one.)

## 4. Set up the production `.env` on the server

Copy your real `.env` over (it has your actual Alpaca/SMTP credentials —
this goes directly server-to-server/Mac-to-server, never through Claude):

```
scp ~/AI-Agents/Stock-Trading/.env root@74.208.78.218:/opt/stock-trading/.env
```

Then SSH in and edit a few values for production
(`nano /opt/stock-trading/.env`):

- `HOST=127.0.0.1` — the app should only listen locally; Caddy (installed
  in step 5) is what actually faces the internet on 80/443 and proxies to
  it. (Your **local/LAN copy** should keep `HOST=auto` — don't change that
  one.)
- `SESSION_COOKIE_SECURE=true` — now that this is served over real HTTPS,
  the login cookie should require it. (Leave this `false` on your LAN
  copy, which is plain HTTP.)
- Everything else (Alpaca keys, SMTP, registration codes, admin emails,
  etc.) can stay as-is.

## 5. Run the bootstrap script

This installs Python 3.13 (via `uv`, so you don't need it from the OS
package manager), creates a dedicated `stockapp` service user, sets up the
systemd service, installs Caddy, and configures automatic HTTPS for
`we-trade.me`:

```
sudo bash /opt/stock-trading/deploy/bootstrap.sh
```

It's safe to re-run if something fails partway — fix the issue and run it
again.

## 6. Verify

```
sudo systemctl status stock-trading
sudo systemctl status caddy
```

Both should show `active (running)`. Then visit **https://we-trade.me** —
you should see the login page with a valid certificate (no browser
warning).

If something's wrong:

```
sudo journalctl -u stock-trading -f    # app logs (crashes, missing .env values, etc.)
sudo journalctl -u caddy -f            # HTTPS/proxy logs (cert issues, DNS not ready yet)
```

Common issues:
- **Caddy can't get a certificate**: DNS for `we-trade.me` hasn't
  propagated yet, or port 80/443 isn't actually reachable — double-check
  IONOS's *own* firewall/security-group panel too (separate from the
  server's own `firewalld`, which `bootstrap.sh` already opened).
- **502 from Caddy**: the app itself isn't running — check
  `systemctl status stock-trading` and its journal above. Often a missing/
  wrong value in `.env` (e.g. `ALPACA_API_KEY`).
- **Connection refused only when Caddy proxies, but the app works when you
  curl it directly on the server**: SELinux may be blocking the proxy
  connection. Try `sudo setsebool -P httpd_can_network_connect 1`.

## Redeploying after code changes

From your Mac:

```
rsync -avz --exclude .venv --exclude __pycache__ --exclude .git \
  --exclude data_store.db --exclude '*.pyc' --exclude .env \
  ~/AI-Agents/Stock-Trading/ root@74.208.78.218:/opt/stock-trading/
ssh root@74.208.78.218 'sudo chown -R stockapp:stockapp /opt/stock-trading && sudo systemctl restart stock-trading'
```

(`--exclude .env` so a redeploy never overwrites the server's production
settings with your local dev `.env`.) If `requirements.txt` changed,
also re-run the dependency install before restarting:

```
ssh root@74.208.78.218 "sudo -u stockapp bash -c 'cd /opt/stock-trading && /usr/local/bin/uv pip install -r requirements.txt'"
```

**Don't skip the `chown`.** `rsync` runs as `root` and writes the new
files as `root`, but the app runs as the unprivileged `stockapp` user
(see `bootstrap.sh`) — without re-chowning, `stockapp` can lose read
access to the freshly-written files (a restrictive root umask can leave
them e.g. `rw-------`), and the service fails to start with something
like:

```
python: can't open file '/opt/stock-trading/main.py': [Errno 13] Permission denied
```

which shows up as Caddy logging `connect: connection refused` (Caddy
itself is fine — the app behind it just never came up). Check
`sudo journalctl -u stock-trading -n 50` if you ever see that.
