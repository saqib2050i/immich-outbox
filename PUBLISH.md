# Getting this onto GitHub and running from a published image

## 1. Create the repo

On github.com, make a new **empty** repository called `immich-outbox`.
No README, no .gitignore, no licence — this folder already has them.

## 2. Push

From this folder:

```bash
git init -b main
git add .
git commit -m "Immich outbox feeder"
git remote add origin git@github.com:YOUR-GITHUB-USERNAME/immich-outbox.git
git push -u origin main
```

Use the `https://github.com/...` URL instead if you don't have SSH keys set up.

## 3. Wait for the build

Push triggers the workflow in `.github/workflows/publish.yml`. It builds for
amd64 and arm64 and publishes to GitHub's own registry as:

```
ghcr.io/YOUR-GITHUB-USERNAME/immich-outbox:latest
```

Watch it under the **Actions** tab. First run takes 3–5 minutes; later ones
are faster because the layers are cached.

Nothing to configure — `GITHUB_TOKEN` is provided automatically and the
workflow already requests `packages: write`.

## 4. Make the package pullable

New GHCR packages are **private** by default, so Unraid can't pull it yet.
Either:

- **Make it public** (simplest for a personal project): your profile →
  Packages → `immich-outbox` → Package settings → Change visibility → Public.
  The image contains no secrets — your Immich key lives in the compose file
  on your server, never in the image.
- **Or keep it private** and log the server in once:
  ```bash
  echo YOUR_TOKEN | docker login ghcr.io -u YOUR-GITHUB-USERNAME --password-stdin
  ```
  using a personal access token with `read:packages`.

## 5. Deploy on Unraid

Edit `docker-compose.yml`, replacing `YOUR-GITHUB-USERNAME`, then paste it
into Dockge as a new stack. Or on the command line:

```bash
mkdir -p /mnt/user/appdata/immich-outbox \
         /mnt/user/photo-outbox \
         /mnt/user/photo-outbox-spool
docker compose up -d
```

No source checkout needed on the server — it pulls the built image.

Dashboard: `http://<unraid>:8099`. Set the Immich address and API key there;
the environment variables in the compose file are only first-run defaults.

## Updating later

```bash
git commit -am "whatever changed" && git push    # CI rebuilds :latest
docker compose pull && docker compose up -d      # on the server
```

For a pinned version instead of `latest`, tag a release — `git tag v1.0.0 &&
git push --tags` — and the workflow also publishes `:1.0.0` and `:1.0`.

## Working on it locally

```bash
docker compose -f docker-compose.dev.yml up --build
```

Builds from source into `./_data`, `./_outbox`, `./_spool`, all gitignored.
