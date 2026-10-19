# ansimin

A self-hosted web interface for administering Ansible inventories, playbooks,
and ad-hoc commands. Run it as a systemd service on a Linux Ansible controller
and access it over HTTPS on your LAN or through Tailscale.

Ansimin is intended for a trusted administrator. It has been tested on a
Raspberry Pi running Raspberry Pi OS 13 (trixie), ARM64. It is early software,
with a single administrator login and no multi-user permissions or audit trail.
There is no packaged installer or published binary release yet.

## Features

- Overview of inventory hosts and groups, with live job output.
- Playbook syntax checks, check mode and execution against a host, group or all hosts.
- Ad-hoc Ansible modules, with optional sudo.
- Playbook creation/editing and INI inventory editing with validation and conflict checks.
- Private file backups with previews, restore, individual deletion and downloads.
- Saved run history, including interrupted jobs after a restart.
- Daily/weekly schedules, initially paused, with timezone and daylight-saving handling.
- Optional encrypted sudo credential for unattended schedules.
- Optional failure notifications through ntfy.sh.
- Tabs that preserve drafts while switching, plus a run indicator across every tab.

Only one job runs at a time. Jobs continue when the browser closes. Cancelling
stops the controller process; remote tasks already started can continue, and
changes are not undone. Ansible check mode predicts supported changes; tasks
that override it can still change systems. Syntax checking does not run tasks,
but loads trusted plugins, includes and playbook content.

## Requirements

- Linux with `/proc` and anonymous memory file support, Python 3.11+, and systemd
  for service installation. macOS can run much of the unit suite, but is not a
  supported execution host for the sudo credential channel.
- `ansible-inventory`, `ansible-playbook` and `ansible` installed together.
- OpenSSL for creating a certificate, or an existing trusted TLS certificate.
- `cryptography` for encrypted scheduled credentials.
- SSH keys, known-host entries and any Ansible collections required by your playbooks,
  available to the account running Ansimin.

On Raspberry Pi OS / Debian, one installation option is:

```sh
sudo apt update
sudo apt install ansible python3-cryptography openssl
```

For a Python virtual environment, install `requirements.txt` there instead;
use that environment's Python in the service. Ansible is a separate prerequisite.

## Installation

Clone this repository, then run the following from its directory. Replace
`YOUR_USERNAME` with an existing Linux administrator account. These instructions
use `/usr/bin/ansible-inventory`; check `command -v ansible-inventory` and adjust
if your installation uses another path, such as Homebrew.

### 1. Install application files

```sh
sudo install -d -m 755 /opt/ansimin /opt/ansimin/web
sudo install -m 644 ansimin.py stored_credentials.py schedules.py notifications.py /opt/ansimin/
sudo install -m 755 become_password.py /opt/ansimin/
sudo install -m 644 web/index.html web/app.js web/style.css /opt/ansimin/web/
```

Keep your Ansible workspace separate from the application. As the account that
will run the service, create a workspace only if you do not already have one:

```sh
mkdir -p "$HOME/ansible"
```

For a new, empty workspace, copy `examples/inventory.ini`,
`examples/ansible.cfg` and `smoke-test.yml` there. Do not overwrite an existing
inventory or configuration. The example inventory targets only the controller
using a local connection. Replace it with your own inventory when ready.
Ansimin reads the workspace's `inventory.ini`; its editor supports INI only.
Root-level `.yml` and `.yaml` files appear in the playbook list.

### 2. Initialize login and TLS

Run as the service account, not root:

```sh
/usr/bin/python3 /opt/ansimin/ansimin.py --init \
  --workspace "$HOME/ansible" --state "$HOME/.local/state/ansimin"
```

This creates a random login password and salted password hash in a private state
directory. Initialization refuses to overwrite an existing login.

Create a certificate using your controller's real hostname and addresses.
Replace the example values below; include its Tailscale address/name if used:

```sh
openssl req -x509 -newkey rsa:3072 -nodes -days 365 \
  -keyout "$HOME/.local/state/ansimin/key.pem" \
  -out "$HOME/.local/state/ansimin/cert.pem" \
  -subj '/CN=controller.example' \
  -addext 'subjectAltName=DNS:controller.example,IP:192.0.2.10'
chmod 600 "$HOME/.local/state/ansimin/"*.pem
```

A self-signed certificate causes a browser warning. Verify the certificate
before trusting it; renew it before expiry. You can instead place a trusted
certificate and private key at those same paths, readable by the service user.

### 3. Configure systemd

Copy `deploy/ansimin.service.example` to a local file, replace **every**
`USERNAME` placeholder, and review the workspace, executable and state paths.
The service user must have access to the workspace, SSH configuration and any
sudo permissions its Ansible tasks require. Do not run the service as root.

```sh
cp deploy/ansimin.service.example /tmp/ansimin.service
# Edit /tmp/ansimin.service before installing it.
sudo install -m 644 /tmp/ansimin.service /etc/systemd/system/ansimin.service
sudo systemctl daemon-reload
sudo systemctl enable --now ansimin
systemctl is-active ansimin
systemctl is-enabled ansimin
```

The example sets a private temporary directory for Ansible, a restrictive file
creation mask and disables core dumps. It intentionally permits the service
account's normal filesystem and sudo access, including local-connection tasks.
More restrictive systemd settings can prevent Ansible from managing the controller.

### 4. Sign in and run a harmless check

As the service user, read the initial password:

```sh
cat "$HOME/.local/state/ansimin/initial-password"
```

Open `https://YOUR_CONTROLLER:8443`. The default listener is all IPv4 interfaces,
port 8443. Configure your firewall and Tailscale access rules for trusted devices;
Ansimin does not install or configure Tailscale. Ports 80 and 443 are untouched.

Select `smoke-test.yml`, choose the `controller` inventory host, and first run a
syntax check. Executing this example prints a message with `changed=0`.
Your app login password is separate from sudo credentials. Store the initial
password in a password manager, then remove `initial-password`. No browser login
password-change or password-reset feature is provided yet.

Command-line options are listed by `python3 ansimin.py --help`:
`--workspace`, `--state`, `--ansible`, `--bind` and `--port`. Default workspace and
state paths are `~/ansible` and `~/.local/state/ansimin`; Ansible is found on PATH.
Use explicit paths in systemd, as shown in the example.

## Interface and execution

**Overview** contains inventory and the latest run. **Playbooks**, **Ad-hoc**,
**Schedules**, **History**, **Backups** and **Settings** group the other controls.
The scheduled credential is under Schedules; notifications are under Settings.
The run indicator's **View run** opens output on Overview. Keyboard users can
move among tabs with Left/Right, Home and End. Tab changes preserve drafts in
memory; reloading or closing the page can discard them.

Manual runs can request a sudo password in a confirmation dialog. For playbooks,
the playbook's `become` settings determine which tasks use sudo. Ad-hoc **Use sudo**
explicitly enables privilege escalation. The password passes through an anonymous
Linux memory file and an executable helper, rather than a disk file, command-line
argument or environment variable containing the password. Exact password matches
are redacted from output; playbooks can still reveal other secrets or transformed
values. Use trusted playbooks and Ansible's `no_log` where appropriate.

Editors accept UTF-8 text up to 256 KiB. Saves validate with Ansible, reject stale
revisions and atomically replace the file. Existing contents are backed up first;
validation/saving/restoration is blocked during active jobs. Inventory validation
previews parsed hosts/groups and rejects empty inventories. It does not verify
SSH connectivity. Playbook edits preserve permission bits but ownership becomes
the service user. Roles, external variable files and vaulted content are outside
the browser editors.

## Backups and history

**Backups** lists earlier file versions. Preview the proposed changes before
**Restore this version**; restoration validates again and backs up the replaced
file. A changed backup or current file requires a fresh preview. Deleted playbooks
can be recreated from backups. **Delete this backup** permanently removes only
the selected version after confirmation. **Download this backup** preserves exact
bytes under the dated `.bak` filename. Downloaded files may contain private data.

Editor backups cover playbooks and `inventory.ini`, not remote machine changes,
credentials, schedules or databases. Backups are private and not automatically
pruned. For a full administrative backup, stop Ansimin and securely copy both the
workspace and state directory. Keep encrypted credentials with their encryption
key; losing that key makes the stored password unrecoverable.

History retains public job metadata and the last 512 Ki characters of output.
Active output is checkpointed periodically, so a crash may lose the final few
seconds. Interrupted jobs are marked and never resumed automatically. Submitted
sudo passwords and module argument fields are excluded from metadata, but Ansible
output can contain arguments or other sensitive information. History and
notification records currently have no automatic pruning.

## Schedules and stored credentials

Daily or weekly schedules use an IANA timezone (initial UI value
`America/Chicago`, editable per schedule). New schedules start paused. **Run once
now** leaves recurrence unchanged. Schedules use the current playbook and
inventory at execution time. There is no ad-hoc scheduling.

The scheduler checks approximately every ten seconds. Missed/offline times and
busy attempts are skipped, not queued. Spring-forward nonexistent times are
skipped; fall-back repeated times run once. A dispatcher claims the occurrence
before starting a job to avoid duplicate maintenance after crashes; an interruption
in that interval can skip the attempt. Review the last outcome before retrying.
Pausing or deleting a schedule does not cancel a job already started.

The optional shared sudo credential uses authenticated Fernet encryption from
`cryptography`. The browser receives only configured status and update time.
`secrets/key` and `secrets/sudo.json` are owner-only files. The running service can
read both: encryption does not protect against compromise of its account or root.
Removal does not erase older administrator backups. Removing a credential blocks
credential-dependent starts but leaves those schedules enabled. No schedules or
credentials ship in this repository.

## Optional ntfy failure notifications

Notifications default to disabled, with no topic. The current implementation
supports `https://ntfy.sh` only. Choose your own topic and configure it once,
while the service is stopped, as the service user:

```sh
sudo systemctl stop ansimin
cd /opt/ansimin
/usr/bin/python3 - <<'PY'
from pathlib import Path
from notifications import Notifications
Notifications(Path.home() / '.local/state/ansimin').configure(
    topic='REPLACE_WITH_YOUR_TOPIC', enabled=True)
PY
sudo systemctl start ansimin
```

Use your actual state path if customized. Subscribe to that case-sensitive topic
in ntfy. Settings provides enable/disable, test delivery and delivery status.
Public unreserved topics may be readable/writable by anyone who knows the topic;
choose accordingly. Authentication and custom ntfy servers are not implemented.

Alerts cover failures, interruptions, missed/busy schedules and dispatcher errors.
They contain only generic reasons and short references, excluding hostnames,
playbook names, command arguments and output. Successful and manually cancelled
jobs are quiet. A separate durable outbox retries transient failures after one
minute and five minutes, with at most three attempts. Permanent HTTP failures
stop immediately. Delivery uses verified HTTPS and rejects redirects. Provider
acceptance does not prove a phone received the notification; lost responses can
produce duplicates. Disabling suppresses pending alerts; an in-flight request may
finish. A stopped or disconnected controller cannot send alerts until it recovers.

## Security and operational limits

A signed-in user can execute arbitrary Ansible modules and edit executable
playbooks with the service user's privileges, plus supplied sudo privileges.
Treat access as administrative access to your fleet. Do not expose Ansimin
directly to the public internet. Use trusted administrators, devices, plugins,
playbooks and inventories. There is one login, no role separation, no MFA and no
built-in public hosting configuration.

Sessions expire after an hour and on restart. Cookies are Secure, HttpOnly and
SameSite=Strict. Write requests require same-origin headers; logins have a global
rate limit. TLS protects browser traffic. The server uses Python's standard-library
HTTP implementation; this project has not undergone an independent security audit.

Runtime state includes the password hash, TLS key/certificate, history and schedule
databases, notification settings/outbox, file backups and optional encrypted sudo
credential. Keep it out of Git and restrict its permissions. The supplied ignore
rules exclude local configuration and common sensitive file types, but always
review what you stage before committing.

## Development and validation

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

Some Linux-only credential tests are skipped on macOS; encrypted-credential tests
require `cryptography`. CI runs on Linux with Python 3.11 and 3.13, installs
Ansible/cryptography and exercises temporary HTTPS backup workflows as well as
the unit suite. The workflow is included; remote CI results are available only
after the repository is pushed.

For integration testing on a Linux development machine with Ansible and OpenSSL:

```sh
python3 -u tests/isolated_check.py --backups-only
# Full isolated checks, including scheduling and captured notifications:
python3 -u tests/isolated_check.py
```

These create a temporary workspace, test login and certificate. The notification
sender is captured rather than sending real alerts. Individual `*_check.py`
scripts target an installation and may start jobs or modify test files; use the
isolated runner unless you have reviewed the relevant script and dedicated test
environment. Those installation checks expect an initial-password file.

## Service management

```sh
systemctl status ansimin
journalctl -u ansimin
sudo systemctl restart ansimin
sudo systemctl disable --now ansimin
```

For updates, finish running jobs, pause imminent schedules, keep a rollback copy
of application files, stop the service, replace source/assets and restart it.
Do not overwrite your workspace or state. Verify login, service health and a
harmless smoke run. Disabling the service leaves your Ansible files intact.

## License

Ansimin is released under the [MIT License](LICENSE).
