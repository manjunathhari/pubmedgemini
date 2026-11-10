# Setting up pubmedgemini on the VPS

For the owner. Do one step at a time. Each step is one command or one click.
No secret is ever written in this file. Type secrets only on the VPS or in
GitHub, never into chat.

Words in angle brackets are yours to fill in:

- `<APP_HOST>`: the web address you chose, for example `quick.example.org`
- `<VPS_ADDRESS>`: the VPS's IPv4 address
- `<VPS_USER>`: the user you log in to the VPS with
- `<GITHUB_USER>`: your GitHub user name

Rule for the whole runbook: the VPS's other services (Traefik and the rest)
are only read, never changed.

Commands marked "on the VPS" run after you log in with
`ssh <VPS_USER>@<VPS_ADDRESS>`. Commands marked "on your computer" run in
your own terminal.

## One-time setup (steps 1 to 9)

### Step 1. Point the web address at the VPS

At your domain provider, add an A record: name `<APP_HOST>`, value
`<VPS_ADDRESS>`. Wait a few minutes.

Check, on your computer:

```
nslookup <APP_HOST>
```

It should show `<VPS_ADDRESS>`.

### Step 2. Read Traefik's names (read only)

You need three values: the entrypoint, the certificate resolver and the Docker
network. They go into `.env` in step 3.

2a. On the VPS, find where Traefik's Compose file is:

```
docker inspect <TRAEFIK_CONTAINER> --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}'
```

2b. On the VPS, print the lines that hold the names:

```
grep -nE 'entrypoint|certresolver|network' <PATH_FROM_2a>
```

Write down:

- the entrypoint (often `websecure`) is `TRAEFIK_ENTRYPOINT`
- the certificate resolver is `TRAEFIK_CERTRESOLVER`
- the Docker network is `TRAEFIK_NETWORK`

Do not edit that file.

### Step 3. Make the folder, the allow list and `.env`

On the VPS, one command per line below.

3a. Make the folder:

```
mkdir -p /opt/pubmedgemini/gate
```

3b. Add each Google address that may sign in (run once per address):

```
echo 'owner@example.com' >> /opt/pubmedgemini/gate/emails.txt
```

3c. Make `.env` private from the start:

```
install -m 600 -o root -g root /dev/null /opt/pubmedgemini/.env
```

3d. Make the database password (the value is not shown) and the database
address that uses it:

```
pw=$(openssl rand -hex 24); printf 'POSTGRES_PASSWORD=%s\nDATABASE_URL=postgresql://postgres:%s@db:5432/postgres\n' "$pw" "$pw" >> /opt/pubmedgemini/.env
```

3e. Make the gate's cookie secret (the value is not shown):

```
echo "OAUTH2_PROXY_COOKIE_SECRET=$(openssl rand -base64 32 | tr -- '+/' '-_')" >> /opt/pubmedgemini/.env
```

3f. Open `.env` and add the remaining names, each as `NAME=value`:

```
nano /opt/pubmedgemini/.env
```

Add these lines (the first three need your values):

```
APP_IMAGE=
APP_HOST=
OWNER_EMAIL=
TRAEFIK_NETWORK=
TRAEFIK_ENTRYPOINT=
TRAEFIK_CERTRESOLVER=
OAUTH2_PROXY_CLIENT_ID=
OAUTH2_PROXY_CLIENT_SECRET=
```

`OWNER_EMAIL` is your own Google address. Leave the last two for step 4.
Leave `APP_IMAGE=` empty; each deploy fills it in. The deploy stops with an
error if this line is missing.

3g. Check the file is private (should start with `-rw-------`):

```
ls -l /opt/pubmedgemini/.env
```

### Step 4. Make the Google sign-in client

In the Google Cloud console:

4a. Open APIs and Services, then OAuth consent screen. Set it up as
"External" and add your allowed addresses as test users.

4b. Open Credentials, then Create credentials, then OAuth client ID.
Type: "Web application".

4c. Under "Authorised redirect URIs" add
`https://<APP_HOST>/oauth2/callback`.

4d. Copy the client ID and secret. On the VPS, open `.env` again and type them
after `OAUTH2_PROXY_CLIENT_ID=` and `OAUTH2_PROXY_CLIENT_SECRET=`:

```
nano /opt/pubmedgemini/.env
```

### Step 5. Make the deploy key

GitHub will log in to the VPS with this key to deploy. It is used for nothing
else.

5a. On your computer, make the key pair (no passphrase, so GitHub can use it):

```
ssh-keygen -t ed25519 -N "" -C pubmedgemini-deploy -f ./pubmedgemini_deploy
```

5b. On your computer, put the public half on the VPS:

```
ssh-copy-id -i ./pubmedgemini_deploy.pub <VPS_USER>@<VPS_ADDRESS>
```

5c. On your computer, save the private half as a GitHub secret (run these from
inside the repo folder):

```
gh secret set VPS_SSH_KEY < ./pubmedgemini_deploy
```

5d. On your computer, delete the private file now that GitHub has it:

```
rm ./pubmedgemini_deploy ./pubmedgemini_deploy.pub
```

5e. On the VPS, print the VPS's own fingerprint:

```
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

5f. On your computer, save the VPS's host key as a secret (this is what keeps
host key checking on):

```
ssh-keyscan -t ed25519 <VPS_ADDRESS> | gh secret set VPS_KNOWN_HOSTS
```

5g. On your computer, check that the fingerprint of what you saved matches 5e:

```
ssh-keyscan -t ed25519 <VPS_ADDRESS> | ssh-keygen -lf -
```

5h. On your computer, save the address (GitHub asks you to type the value):

```
gh secret set VPS_HOST
```

5i. On your computer, save the user name (type the value when asked):

```
gh secret set VPS_USER
```

`<VPS_USER>` must be able to write in `/opt/pubmedgemini` and run `docker`.
The plan keeps `.env` owned by root, so this user is root in practice. If you
would rather use a separate user, tell me before you start; it changes step 3.

### Step 6. Let the VPS pull the image

6a. In GitHub, make a token that can only read packages: Settings, Developer
settings, Personal access tokens (classic), tick only `read:packages`.

6b. On the VPS, log in. It asks for the token; the typing is hidden:

```
docker login ghcr.io -u <GITHUB_USER>
```

### Step 7. Firewall (your choice; skip if you prefer)

This keeps only ports 22, 80 and 443 open. Turning it on is your decision.
The order below allows SSH first, so you cannot lock yourself out.

7a. On the VPS, check which port SSH really uses (this should be 22):

```
ss -tlnp | grep sshd
```

If it shows a different port, stop here and tell me. Use that port in 7b.

7b. Allow SSH first:

```
ufw allow 22/tcp
```

7c. Allow web traffic:

```
ufw allow 80/tcp
```

7d. Allow secure web traffic:

```
ufw allow 443/tcp
```

7e. Read the list before you turn it on. It must show 22, 80 and 443:

```
ufw show added
```

7f. Turn it on (only now):

```
ufw enable
```

7g. Open a second terminal and log in to the VPS again. If that works, you are
safe. If it does not, in the first terminal run `ufw disable`.

```
ssh <VPS_USER>@<VPS_ADDRESS>
```

Note: other services on the VPS that use other ports will be blocked by this
firewall. Docker's own published ports can bypass it. Check before you turn it
on.

### Step 8. Nightly backup

02:30 IST is 21:00 UTC. The VPS clock is usually UTC.

On the VPS:

```
echo '0 21 * * * root /opt/pubmedgemini/backup.sh >> /var/log/pubmedgemini-backup.log 2>&1' > /etc/cron.d/pubmedgemini
```

`backup.sh` arrives on the VPS with the first deploy. The first backup runs the
night after that. It keeps 7 days in `/var/backups/pubmedgemini/`.

### Step 9. Check it works

This comes after the first deploy. A deploy only happens when you tell me
"merge it" and the pull request is merged into `main`.

9a. Open `https://<APP_HOST>` in your browser and sign in with an allowed
address. You should see "Signed in as ...".

9b. Open the same address in a private window and sign in with an address that
is not on the list. Google's sign-in should refuse it.

9c. On the VPS, run the backup once by hand:

```
/opt/pubmedgemini/backup.sh
```

It should print `backup ok:` and a file name.

## Rollback (go back to the previous version)

Rollback changes the app code only. It does not undo database changes.

1. On the VPS, go to the folder:

```
cd /opt/pubmedgemini
```

2. List the image versions the VPS still has (newest first):

```
docker images --format '{{.CreatedAt}}  {{.Repository}}:{{.Tag}}' | grep pubmedgemini | sort -r
```

3. Look at which one `.env` uses now:

```
grep '^APP_IMAGE=' .env
```

4. Put the previous one in. Replace `<PREVIOUS_IMAGE>` with the full name from
step 2 (it ends with a long commit code):

```
sed -i 's|^APP_IMAGE=.*|APP_IMAGE=<PREVIOUS_IMAGE>|' .env
```

5. Start it:

```
docker compose up -d
```

6. Check that `app` says `healthy`:

```
docker compose ps
```

The deploy also does this by itself (with the previous `compose.yaml` too) if
the new version does not become healthy, or the gate is not running, within
120 seconds. The GitHub deploy then shows as failed.

## Restore from a backup

Backups are in `/var/backups/pubmedgemini/`, one file per day. Restoring
replaces the current data with the file's data.

1. On the VPS, go to the folder:

```
cd /opt/pubmedgemini
```

2. List the backups and pick a date:

```
ls -l /var/backups/pubmedgemini/
```

3. Make a fresh backup of the current state first, so you can undo:

```
BACKUP_DIR=/var/backups/pubmedgemini-before-restore ./backup.sh
```

4. Stop the app and the gate (the database keeps running):

```
docker compose stop gate app
```

5. Restore. Replace `<DATE>` with the date of the file you picked:

```
docker compose exec -T db pg_restore -U postgres -d postgres --clean --if-exists < /var/backups/pubmedgemini/<DATE>.dump
```

6. Start them again:

```
docker compose start app gate
```

7. Check that `app` says `healthy`, then open `https://<APP_HOST>`:

```
docker compose ps
```

Note: step 3 saves the current state in its own folder, so it never overwrites
a nightly backup.
