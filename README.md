# unifi2ipam

Synchronizes network client data from a UniFi Network Controller to a [phpIPAM](https://phpipam.net/) instance. The script reads all active clients from a UniFi site and creates or updates their address records in phpIPAM, using the MAC address as the stable identifier across IP changes.

> **Behaviour change (1.2.0):** phpIPAM is treated as the source for DNS, so the tool now writes curated DNS names, marks the rows it creates and never touches any other row. `--nuke-and-pave` is removed. See below.

---

## How It Works

For each *connected* client that UniFi reports with an IP and MAC inside the configured subnets, the tool reconciles one phpIPAM row. UniFi is read first (clients from the integration API; alias and Local DNS Record from the classic `rest/user` API, which is the only place they exist). If either read fails or comes back empty, **nothing is written to phpIPAM and the run exits 1**.

### What is written

| phpIPAM field | Value |
| --- | --- |
| `hostname` | The curated DNS name, or **empty** when the client has none (see below) |
| `description` | UniFi's display name (first 60 characters), so the inventory value is kept |
| `owner` | `unifi2ipam` on every row the tool creates |
| `note` | `Created by UniFi2IPAM` (the v1.0.x marker; rows carrying it are adopted as ours) |
| `lastSeen` | UTC time of the run, set for every connected client on every run |
| `mac`, `ip`, `subnetId` | As reported by UniFi |

No phpIPAM custom field or tag is needed.

### Names

The curated name is, in order: the client's UniFi **Local DNS Record** (when enabled), else its **alias**.

- A Local DNS Record must sit in the configured zone (`--dns-zone`, e.g. `home.8none1.com`) and is stored without it: `pet-cam.iot.home.8none1.com` becomes `pet-cam.iot`. At most two labels remain; every label must be lowercase `a-z 0-9 -`. A record outside the zone, or invalid, yields no name (it does *not* fall back to the alias). Without a zone, records are not used.
- An alias counts only if it is already a single valid label (`k3s-server-01`, `Bambu-P1S` becomes `bambu-p1s`). `Living Room TV`, `Adam's iPhone`, `SonosZP 16:0c` or a bare MAC are display names and are never repaired into a DNS name.
- A client with a **randomised (locally administered) MAC** never gets a name, whatever it has configured.
- A name claimed by two connected clients goes to neither.

**Empty `hostname` is the "no forward name" marker.** A consumer should create a forward record only for rows with `owner = unifi2ipam`, a non-empty `hostname` and a recent `lastSeen`.

### Ownership

The tool only updates or deletes rows it owns (`owner = unifi2ipam`, or the v1.0.x note). For each client:

1. The address is held by a row that is not ours: **left alone** (logged; the run still succeeds).
2. Our row has the MAC at this address: PATCH what differs (a rename arrives here) plus `lastSeen`.
3. Our row for a different MAC holds the address (a device left it): the row is reused for the new device.
4. Nothing holds the address: a row is created. If our row for this MAC exists at another address it is deleted *after* the create succeeded (phpIPAM cannot change a row's IP).

Rows of clients that have disconnected are not changed and never deleted; their `lastSeen` simply stops moving. v1.0.x deleted whatever row held an address when a create reported a conflict; that code is gone.

### Exit status and dry run

- Exit 0 only if every client was processed. Any phpIPAM lookup that is not a clean answer (no response, 5xx, 401, bad JSON) is an error for that client: it is never read as "not found".
- `--dryrun` reads UniFi **and** phpIPAM and logs each `[dry-run] would POST/PATCH/DELETE ...`. The guard sits in the request function, so no code path can send a write.

---

## Prerequisites

- Python 3.10 or later
- [uv](https://docs.astral.sh/uv/) for dependency management
- A UniFi Network Controller with API access enabled
- A phpIPAM instance with an API application configured

---

## Installation

```bash
git clone https://github.com/<you>/unifi2ipam.git
cd unifi2ipam
uv sync
```

---

## Configuration

Configuration is loaded from three sources in order of increasing precedence:

```text
config file  →  environment variables  →  CLI flags
```

### Config file (recommended)

Default location: `/etc/unifi2ipam/config.yaml`
Override with: `--config PATH`

```yaml
unifi:
  url: "https://unifi.local/proxy/network/integration/v1/"
  # api_key: "..."  # use UNIFI_API_KEY env var instead

ipam:
  base_url: "https://ipam.local"
  app_id: "your_app_id"
  # api_key: "..."  # use IPAM_API_KEY env var instead

# insecure: true  # uncomment to disable SSL certificate verification
```

### Environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `UNIFI_API_KEY` | Yes | UniFi API key |
| `IPAM_API_KEY` | Yes | phpIPAM application token |
| `UNIFI_URL` | No* | UniFi controller base URL |
| `IPAM_BASE_URL` | No* | phpIPAM host URL |
| `IPAM_APP_ID` | No* | phpIPAM application ID |
| `DNS_ZONE` | No | Zone of the Default network, e.g. `home.8none1.com` |
| `UNIFI_LEGACY_URL` | No | Classic API base, derived from `UNIFI_URL` (`.../proxy/network/`) when omitted |
| `UNIFI_SITE_NAME` | No | Classic API site name (default `default`) |

*Required if not set in config file or via CLI flags.

### CLI flags

| Flag | Description |
| --- | --- |
| `--unifi-url URL` | UniFi controller base URL |
| `--ipam-base-url URL` | phpIPAM base URL |
| `--ipam-app-id ID` | phpIPAM application ID |
| `--dns-zone ZONE` | Zone stripped from Local DNS Records (config: `dns.zone`) |
| `--unifi-site-name NAME` | Classic API site name (config: `unifi.site_name`) |
| `--insecure` | Disable SSL certificate verification (opt-in, for self-signed certs) |
| `--config PATH` | Path to YAML config file |

---

### Generating a UniFi API Key

Log in to the UniFi dashboard with a sufficiently privileged account and go to **Settings → Control Plane → Integrations**. Click **Create API Key**, give it a name, set an optional expiry, and copy the key. Use this as `UNIFI_API_KEY`.

The UniFi API endpoint is typically: `https://<controller-ip>/proxy/network/integration/v1/`

### Generating a phpIPAM API Token

Log in as an Administrator and go to **Administration → API → Create API key**. Set an App ID (this becomes `IPAM_APP_ID`), copy the generated App code, and set the security mode to **SSL with App code token**. Use the App code as `IPAM_API_KEY`.

---

## Usage

```bash
# Verify connectivity and preview what would be synced (no changes made)
uv run unifi2ipam.py --dryrun

# Standard incremental sync
uv run unifi2ipam.py

# Use self-signed certificates
uv run unifi2ipam.py --insecure

# Specify a site directly (skips the interactive site-selection prompt)
uv run unifi2ipam.py --site-id <site-id>

# Verbose output (shows each API request)
uv run unifi2ipam.py --dryrun --verbose

# Quiet mode (errors only) with log file
uv run unifi2ipam.py --quiet --log-file /var/log/unifi2ipam.log
```

Full option reference (`uv run unifi2ipam.py --help`):

```text
options:
  --limit LIMIT        Maximum number of clients to fetch from UniFi (default: 1000).
  --site-id ID         UniFi site ID. If omitted, available sites are listed for selection.
  --dryrun             Read UniFi and phpIPAM, print what would change, write nothing.
  --dns-zone ZONE      Zone stripped from Local DNS Records.
  --unifi-site-name N  Classic API site name (default: default).
  --workers WORKERS    Parallel workers for client processing (default: 4).
  --config PATH        Path to YAML config file.
  --unifi-url URL      UniFi controller base URL.
  --ipam-base-url URL  phpIPAM base URL.
  --ipam-app-id ID     phpIPAM application ID.
  --insecure           Disable SSL certificate verification.
  --verbose            Enable debug-level output.
  --quiet              Suppress all output except errors.
  --log-file PATH      Write logs to file in addition to stdout.
  --version            Show version and exit.
```

---

## Cron Job Setup

For automated periodic syncs, add an entry to your crontab. Use `--site-id` to avoid the interactive site-selection prompt and `--quiet` to limit output to errors only:

```cron
# Sync UniFi clients to phpIPAM every hour
0 * * * * UNIFI_API_KEY=your_key IPAM_API_KEY=your_key /path/to/uv run /path/to/unifi2ipam/unifi2ipam.py --site-id <site-id> --quiet --log-file /var/log/unifi2ipam.log 2>&1
```

Find your site ID with `uv run unifi2ipam.py --dryrun` first.

---

## Container Deployment

Pre-built images are published to the GitHub Container Registry on every push to `main` and on version tags:

```text
ghcr.io/adamherbert/unifi2ipam:main      # latest from main
ghcr.io/adamherbert/unifi2ipam:1.0.1     # specific version
```

### Docker

Pass credentials as environment variables and mount your `config.yaml`:

```bash
docker run --rm \
  -e UNIFI_API_KEY=your_unifi_key \
  -e IPAM_API_KEY=your_ipam_key \
  -v /path/to/config.yaml:/etc/unifi2ipam/config.yaml:ro \
  ghcr.io/adamherbert/unifi2ipam:main \
  --site-id <site-id> --quiet
```

### Kubernetes CronJob

Create a `Secret` for credentials and a `ConfigMap` for the config file, then deploy as a `CronJob`:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: unifi2ipam-secrets
  namespace: unifi2ipam
type: Opaque
stringData:
  UNIFI_API_KEY: "your_unifi_api_key"
  IPAM_API_KEY: "your_ipam_api_key"
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: unifi2ipam-config
  namespace: unifi2ipam
data:
  config.yaml: |
    unifi:
      url: "https://unifi.local/proxy/network/integration/v1/"
    ipam:
      base_url: "https://ipam.local"
      app_id: "your_app_id"
    # insecure: true
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: unifi2ipam
  namespace: unifi2ipam
spec:
  schedule: "0 * * * *"          # every hour
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: unifi2ipam
              image: ghcr.io/adamherbert/unifi2ipam:main
              args: ["--site-id", "<site-id>", "--quiet"]
              env:
                - name: UNIFI_API_KEY
                  valueFrom:
                    secretKeyRef:
                      name: unifi2ipam-secrets
                      key: UNIFI_API_KEY
                - name: IPAM_API_KEY
                  valueFrom:
                    secretKeyRef:
                      name: unifi2ipam-secrets
                      key: IPAM_API_KEY
              volumeMounts:
                - name: config
                  mountPath: /etc/unifi2ipam
                  readOnly: true
          volumes:
            - name: config
              configMap:
                name: unifi2ipam-config
```

Find your site ID first using a dry run (replace image pull with your actual values):

```bash
docker run --rm \
  -e UNIFI_API_KEY=your_key \
  -e IPAM_API_KEY=your_key \
  -v /path/to/config.yaml:/etc/unifi2ipam/config.yaml:ro \
  ghcr.io/adamherbert/unifi2ipam:main --dryrun
```

---

## Notes

- SSL certificate verification is **enabled by default**. Use `--insecure` (or `insecure: true` in the config file) for self-signed certificates.
- Only clients with both `ipAddress` and `macAddress` fields populated are processed. Clients without a DHCP lease are skipped.
- Subnets must already exist in phpIPAM. The script will not create subnets — if no matching subnet is found for a client's IP, that client fails with an error and the run exits 1.
- Tests: `python -m unittest discover -s tests` (both APIs are stubbed; no network).
- Client processing is parallelised using a thread pool (`--workers`, default 4). 

---

## To Do

- [ ] Log output to a file rather than stdout only *(use `--log-file` as a workaround)*

## Problems

Please open a [GitHub Issue](../../issues) and it will be addressed as time permits.
