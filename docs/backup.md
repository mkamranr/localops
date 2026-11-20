# Backup and restore

LocalOps backups are two files that you store apart from each other:

| File | Contents | Protection |
|---|---|---|
| `localops-backup-<ts>.tar.gz` | PostgreSQL dump, Qdrant vector snapshots, configuration, manifest, checksums | mode 600; contains your operational data |
| `localops-keys-<ts>.enc` | `APP_SECRET_KEY`, `LOCALOPS_ENCRYPTION_KEY`, database passwords, bootstrap password, TLS private key | sealed with a passphrase (scrypt + AES-256-GCM) |

Stored credentials (SSH keys, passwords, API tokens, model API keys) are encrypted in the database with `LOCALOPS_ENCRYPTION_KEY`.

- If you lose that key, every stored credential has to be re-entered.
- Anyone with the key and the backup can decrypt them.

That's why the key is kept in a separate, passphrase-sealed file. A backup never silently leaves it out.

## What is backed up

| Data | How |
|---|---|
| PostgreSQL (tenants, users, inventory, incidents, investigations, knowledge documents and their versions, policies, approvals, audit log) | `pg_dump -Fc`, a consistent snapshot |
| Qdrant (knowledge vectors) | one snapshot per collection; skip with `--no-vectors` and re-index after the restore |
| Configuration | compose files, nginx configuration, TLS certificate, `.env` with secrets redacted |
| Not backed up | Redis (queues and locks only), container images (they are in the bundle), model weights (your model server) |

Uploaded knowledge files are stored as text in PostgreSQL, so they're included in the dump.

## Taking a backup

```bash
/opt/localops/scripts/backup.sh                       # prompts for the keys passphrase
/opt/localops/scripts/backup.sh --keys-passphrase-file /root/.localops-keys-pass
```

- Output goes to `/opt/localops/backups/` (mode 700); change it with `--out DIR`.
- The newest 7 backups and keys files are kept; change this with `--keep N`, where `0` keeps everything.
- The passphrase must be at least 12 characters. Keep it in your password manager. The keys file can't be opened without it.

### Scheduled backups

To avoid storing the passphrase on the server, seal the keys once and schedule backups without a keys file:

```bash
/opt/localops/scripts/backup.sh --keys-only           # once, and after any key change
# move the keys file off the server, then:
crontab -e
15 2 * * *  /opt/localops/scripts/backup.sh --keys-managed-elsewhere --out /srv/backups/localops >> /var/log/localops-backup.log 2>&1
```

`--keys-managed-elsewhere` is recorded in the backup manifest, and `restore.sh` warns about it.

### Verifying a backup

```bash
/opt/localops/scripts/backup.sh --verify /opt/localops/backups/localops-backup-20261120T021500Z.tar.gz
```

This checks the archive checksums, that `pg_restore` can read the dump, and that every vector snapshot is intact.

Test-restore regularly on a separate host as well. The air-gap smoke workflow does a full backup, wipe, reinstall and restore on every release.

## Restoring

`restore.sh` restores into an existing or freshly installed LocalOps of the same or a newer version. Newer versions migrate the restored database automatically.

```bash
# Same server, keys unchanged
/opt/localops/scripts/restore.sh backups/localops-backup-<ts>.tar.gz

# New server / disaster recovery
sudo scripts/install.sh --non-interactive ...          # from the offline bundle
/opt/localops/scripts/restore.sh localops-backup-<ts>.tar.gz --keys localops-keys-<ts>.enc
```

Restore does the following:

1. Verifies the archive checksums.
2. With `--keys`, unseals the keys file and restores `APP_SECRET_KEY`, `LOCALOPS_ENCRYPTION_KEY` and the TLS certificate and key. The new installation's own database passwords are kept, because PostgreSQL roles aren't part of the dump.
3. Stops the application services, drops the database and recreates it from the dump with `pg_restore --create`, which restores owners and privileges.
4. Restores each Qdrant collection snapshot.
5. Starts everything, applies migrations and waits for health checks.
6. Runs `check-secrets`: every stored credential must decrypt with the configured key. If the key is wrong, the restore reports exactly which credentials can't be read, and you can run it again with the right keys file.

`--yes` skips the confirmation prompt, for automation.

## Disaster recovery runbook

1. Provision a Linux x86_64 host with Docker. See [airgap-installation.md](airgap-installation.md).
2. Install the same or a newer LocalOps version from its offline bundle.
3. Copy the latest backup archive and keys file to the host. Have the passphrase ready.
4. Run `scripts/restore.sh <archive> --keys <keys file>`.
5. Sign in with your normal accounts. They come back from the backup.
6. If the backup was taken with `--no-vectors`, open **Knowledge** and re-index.
7. Run `scripts/security-check.sh`, then take a fresh backup.

## Tenant export

To export one tenant's data as JSON, for example for a tenant leaving the platform or for an audit request:

```bash
cd /opt/localops
docker compose exec -T api python -m app.cli export-tenant <tenant-slug> > tenant-export.json
```

- **Included:** resources, relationships, integrations (without credentials), incidents, investigations and their events, knowledge documents and versions, policies, approvals, users and roles, and the audit log.
- **Excluded:** credentials, password hashes, session tokens, derived search chunks and metric samples.
- **Isolation:** the export runs under the tenant's row-level-security context, so it can't contain another tenant's data.
