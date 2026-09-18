#!/usr/bin/env bash
# Ship the working tree to the server and restart what needs restarting.
#
# The database is never touched: it lives on the server and is the thing being collected into.
# Secrets are never touched either — .env is written once at provision time and stays put.
set -euo pipefail

HOST="${RADAR_HOST:?set RADAR_HOST=root@your-server (the machine deploy/README.md was provisioned on)}"
KEY="${RADAR_KEY:-$HOME/.ssh/fomoradar}"
APP=/opt/fomoradar/app

echo "==> packing"
tar --exclude='.venv' --exclude='node_modules' --exclude='dist' --exclude='.git' \
    --exclude='.astro' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='*.egg-info' --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='*scored*.json' --exclude='*.har' --exclude='fomo_state' \
    --exclude='radar.html' --exclude='report.md' --exclude='.env' --exclude='.fonts' \
    -czf /tmp/radar-app.tgz .

echo "==> uploading"
scp -q -i "$KEY" /tmp/radar-app.tgz "$HOST:/tmp/radar-app.tgz"

echo "==> installing"
ssh -i "$KEY" "$HOST" bash -s <<'REMOTE'
set -euo pipefail
APP=/opt/fomoradar/app
# .env is preserved across every deploy: it is the one file the server owns, not the repo
cp "$APP/.env" /tmp/.env.keep
tar -xzf /tmp/radar-app.tgz -C "$APP"

# Unpacking over a directory adds and replaces; it never removes. So a file deleted in the repo
# lived on here indefinitely, and the server went on importing modules that no longer exist
# anywhere else - 1,165 lines of them, plus a test file whose absence locally is why the two
# machines disagreed about how many tests this project has. Anything under a directory the repo
# owns entirely, and not in the archive just unpacked, is gone from the repo and goes from here.
tar -tzf /tmp/radar-app.tgz | sed 's#^\./##' | grep -v '/$' | LC_ALL=C sort > /tmp/shipped.txt
for d in fomo_agent tests deploy docs extension site/src site/public; do
  [ -d "$APP/$d" ] || continue
  ( cd "$APP" && find "$d" -type f ) | LC_ALL=C sort > /tmp/present.txt
  LC_ALL=C comm -13 /tmp/shipped.txt /tmp/present.txt | while read -r f; do
    case "$f" in *__pycache__*) ;; *) echo "   deleted in the repo, removing: $f";; esac
    rm -f "$APP/$f"
  done
done
rm -f /tmp/shipped.txt /tmp/present.txt
find "$APP/fomo_agent" "$APP/tests" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# Windows line endings do not survive contact with a shell. The repo is edited on a Windows box,
# git converts on checkout, and any tool that writes a file with platform newlines puts them back
# - which is how install-extension.sh once failed with `set: pipefail: invalid option name`, a
# message that reads like a bash bug and is a carriage return. Normalising here rather than
# trusting the packing machine means no future edit anywhere can break a deploy this way.
# The carriage return is built with printf rather than written as an escape, because a literal one
# in this file would be stripped by the very normalising this line performs.
CR=$(printf '\015')
find "$APP/deploy" -type f \( -name '*.sh' -o -name '*.service' -o -name '*.timer' \
     -o -name '*.conf' -o -name 'Caddyfile' \) -exec sed -i "s/${CR}\$//" {} +
install -o radar -g radar -m 600 /tmp/.env.keep "$APP/.env"
rm -f /tmp/.env.keep /tmp/radar-app.tgz
chown -R radar:radar "$APP"

# Unit files ship in the repo but run from /etc/systemd/system, and until now every one of them
# had to be copied across by hand - which is how a fix lands in the tree and never reaches the
# machine. Anything already installed is refreshed here. Anything shipped but not installed is
# named rather than quietly enabled: starting a unit is a decision, not a deploy step.
changed=""
for f in "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer; do
  u=$(basename "$f")
  if [ ! -f "/etc/systemd/system/$u" ]; then
    echo "   shipped, not installed: $u"
  elif ! cmp -s "$f" "/etc/systemd/system/$u"; then
    cp "$f" "/etc/systemd/system/$u"
    changed="$changed $u"
    echo "   updated $u"
  fi
done
if [ -n "$changed" ]; then
  systemctl daemon-reload
  # A reload re-reads a timer but does not re-arm one that is already running, so a changed
  # interval would take effect at the next reboot and nowhere else.
  for u in $changed; do
    case "$u" in *.timer) systemctl is-active --quiet "$u" && systemctl restart "$u";; esac
  done
fi

cd "$APP"
sudo -u radar /opt/fomoradar/venv/bin/pip install -q -e ".[api,dev]"
sudo -u radar /opt/fomoradar/venv/bin/python -m pytest -q 2>&1 | tail -1

cd "$APP/site"
sudo -u radar npm install --silent --no-fund --no-audit
# Astro bakes `site` into the SSR bundle at build time, so the canonical, og:url and og:image
# addresses are decided here rather than by the unit file. Without this the pages ship claiming
# whatever the config default happens to be, which is a domain we do not serve.
SITE_URL="$(sed -n 's/^PUBLIC_SITE_URL=//p' "$APP/.env" | tail -1)"
REPO_URL="$(sed -n 's/^PUBLIC_REPO_URL=//p' "$APP/.env" | tail -1)"
TOKEN_CA="$(sed -n 's/^PUBLIC_TOKEN_CA=//p' "$APP/.env" | tail -1)"
TOKEN_SYMBOL="$(sed -n 's/^PUBLIC_TOKEN_SYMBOL=//p' "$APP/.env" | tail -1)"
FOMO_REF_CODE="$(sed -n 's/^FOMO_REF_CODE=//p' "$APP/.env" | tail -1)"
FOMO_REF_PARAM="$(sed -n 's/^FOMO_REF_PARAM=//p' "$APP/.env" | tail -1)"
FOMO_REF_URL="$(sed -n 's/^FOMO_REF_URL=//p' "$APP/.env" | tail -1)"
TG_BOT="$(sed -n 's/^TELEGRAM_BOT_NAME=//p' "$APP/.env" | tail -1)"
sudo -u radar env PUBLIC_SITE_URL="$SITE_URL" PUBLIC_REPO_URL="$REPO_URL"   PUBLIC_TOKEN_CA="$TOKEN_CA" PUBLIC_TOKEN_SYMBOL="${TOKEN_SYMBOL:-FOMOBRAIN}"   PUBLIC_TELEGRAM_BOT="${TG_BOT:-fomoradarRH_bot}"   PUBLIC_FOMO_REF_CODE="$FOMO_REF_CODE" PUBLIC_FOMO_REF_PARAM="${FOMO_REF_PARAM:-ref}" PUBLIC_FOMO_REF_URL="$FOMO_REF_URL"   npm run build 2>&1 | grep -E "error|Complete!" | tail -1

# The site starts After= the api, so the pair restarts as one and the public gap is the api's
# stop plus one node start - a few seconds. The bot and the receiver follow on their own; a
# Telegram poll that has to be re-issued is not something anybody sees.
systemctl restart radar-api radar-site
systemctl restart radar-bot radar-receive radar-watch
sleep 5
for u in radar-api radar-site radar-bot radar-watch; do printf '   %-12s %s\n' "$u" "$(systemctl is-active $u)"; done
REMOTE

echo "==> checking"
# The bare IP now redirects to the canonical host, so checking it would only ever prove that the
# redirect works. Ask the address the site itself claims to be.
SITE=$(ssh -i "$KEY" "$HOST" "sed -n 's/^PUBLIC_SITE_URL=//p' $APP/.env | tail -1")
SITE=${SITE:-http://${HOST#*@}}
for p in / /leaderboard /api/health; do
  printf '   %s  %s%s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$SITE$p")" "$SITE" "$p"
done
echo "==> done"
