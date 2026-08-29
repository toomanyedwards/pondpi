# One-time Pi setup

Run these on the Raspberry Pi itself (over SSH or a console). They only need
to be done once; after this, pushes to `main` deploy automatically.

## 1. Deploy directory + venv

```bash
sudo mkdir -p /opt/pondpi
sudo chown "$USER":"$USER" /opt/pondpi
python3 -m venv /opt/pondpi/.venv
```

## 2. systemd service

```bash
sudo cp deploy/pondpi.service /etc/systemd/system/pondpi.service
sudo systemctl daemon-reload
sudo systemctl enable pondpi.service
```

(Leave it stopped for now — it'll start once the first deploy has synced
`read_sensor.py` into `/opt/pondpi`.)

## 3. Passwordless service restart for the runner

The deploy workflow runs `sudo systemctl restart pondpi.service`. Allow the
runner's user to do that without a password prompt:

```bash
echo "$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart pondpi.service" | sudo tee /etc/sudoers.d/pondpi-deploy
sudo chmod 440 /etc/sudoers.d/pondpi-deploy
```

## 4. Register the self-hosted GitHub Actions runner

In the repo on GitHub: **Settings → Actions → Runners → New self-hosted
runner**, choose Linux/ARM64 (or ARM, depending on your Pi), and follow the
generated commands — they look like:

```bash
mkdir actions-runner && cd actions-runner
curl -o actions-runner.tar.gz -L <url-from-github>
tar xzf actions-runner.tar.gz
./config.sh --url https://github.com/toomanyedwards/pondpi --token <token-from-github>
sudo ./svc.sh install
sudo ./svc.sh start
```

`sudo ./svc.sh install` runs the runner as a service so it survives reboots
and doesn't need a logged-in terminal.

## 5. First deploy

Merge a PR into `main` (or re-run the queued "Deploy" workflow run if one is
already waiting). The `deploy` job will sync files into `/opt/pondpi`,
install dependencies, and restart `pondpi.service`.

Check it's running:

```bash
systemctl status pondpi.service
journalctl -u pondpi.service -f
```
