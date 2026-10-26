## 4. `docs/OCI_Registry_Allowlist.md`

docs/OCI_Registry_Allowlist.md

# OCI Registry Allowlist and Image Pinning Procedure

**Status:** Slice 16 deliverable. Authoritative procedure for the container-image source class that sits alongside the mod-artifact source class.
**Applies to:** spec v1.4.0 §3.2 (constraints), §5.2 (`[runtime].image_digest`), §12.4 stage 18 (health checks against pinned images), §12.8 (runtime staging).
**Rehearsal:** part of the Migration Runbook Phase 2 preconditions.

---

## 1. Why

Production MUST NOT contact mod providers, but production MUST pull container images for the Minecraft runtime. These are distinct source classes:

- **Mod artifacts** — pulled by DEV from Modrinth/CurseForge and delivered to prod inside signed release bundles. Prod never contacts the provider.
- **Container images** — pulled by prod directly from an OCI registry. This is a **separately authorized source class**.

Without a strict allowlist, "pull the latest `itzg/minecraft-server`" is an unconstrained supply-chain exposure: a compromised registry account, a MITM, or a typo-squatted image would compromise prod. This procedure closes that exposure.

---

## 2. Currently allowed registries

Only the following registries are authorized for prod image pulls. Any image reference that does not resolve to one of these MUST be rejected at deployment time.

| Registry | Hostname | Current use | Approval |
|---|---|---|---|
| Docker Hub | `docker.io` (canonical: `registry-1.docker.io`) | `itzg/minecraft-server` (primary runtime) | approved (baseline) |
| GitHub Container Registry | `ghcr.io` | Reserved — not currently used | not yet approved |
| Quay.io | `quay.io` | Reserved — not currently used | not yet approved |

Adding a registry to this table is an **operator action** that requires:

1. A written justification (why the default registry cannot serve the need).
2. The registry's TLS pinning / signature verification posture.
3. Explicit update to this document via PR.
4. Corresponding update to prod's docker daemon egress firewall rules (see §4).

**Do not pull from any other registry on prod, including private registries, mirrors, and caches.**

---

## 3. Pin-by-digest procedure (DEV)

Tags are mutable. `itzg/minecraft-server:java25` today may be a different image tomorrow. To defeat tag mutation, prod references images **by immutable digest**, not by tag.

### 3.1 Resolving the tag to a digest (DEV)

On **DEV**, before publishing the slip that will be deployed:

```bash
# Pull the tag so the digest is resolvable locally.
docker pull itzg/minecraft-server:java25

# Resolve the digest. Multiple lines are possible for multi-arch images;
# the first line corresponds to the platform you pulled on.
docker image inspect --format '{{index .RepoDigests 0}}' itzg/minecraft-server:java25
# Example output: itzg/minecraft-server@sha256:abc123...
```

Record this digest. It is what `slip.runtime.image_digest` will record.

### 3.2 Recording the digest in the slip

`minecraft-upgrade materialize` (`materialize.py::_resolve_image_digest`) already does this automatically when Docker is reachable on DEV:

```python
raw = _run_docker_inspect(image, "{{index .RepoDigests 0}}")
# raw is "itzg/minecraft-server@sha256:abc123..."
# _resolve_image_digest strips the repository and returns "sha256:abc123..."
```

If Docker is unreachable on DEV (`MINECRAFT_UPGRADE_NO_DOCKER=1`), `_resolve_image_digest` returns `None` and `materialize` records a placeholder digest (`sha256:` + 64 zeros). **A slip with a placeholder digest is not prod-deployable** — the operator MUST ensure a real digest is recorded before promoting the slip.

### 3.3 Verification

Before promoting any slip, verify the digest resolved correctly:

```bash
# On DEV
grep 'image_digest' sync/packing_slips/<candidate>.toml
# Expected: image_digest = "sha256:<64-hex>"  (NOT "sha256:0000...0000")

# Confirm the digest is authoritative (pull by digest works).
docker pull itzg/minecraft-server@sha256:<64-hex>
# Expected: "Status: Image is up to date" or a successful pull.
```

If the digest is the placeholder, re-run `materialize` with Docker reachable. Do not promote.

---

## 4. Prod daemon egress policy

Prod's docker daemon MUST be configured to pull only from the allowlisted registries. This is enforced **outside** the upgrade toolchain, at the network layer.

### 4.1 Docker daemon configuration

Add to `/etc/docker/daemon.json` on prod:

```json
{
  "registry-mirrors": [],
  "insecure-registries": [],
  "block-registry": [
    "quay.io",
    "ghcr.io",
    "mcr.microsoft.com",
    "public.ecr.aws"
  ]
}
```

Note: `block-registry` is available on Docker 25.0+. On older daemons, use firewall rules instead (see §4.2).

Restart docker after editing:

```bash
sudo systemctl restart docker
```

### 4.2 Firewall egress rules (fallback)

If `block-registry` is not available, enforce via iptables/nftables on the prod host:

```bash
# Allow egress to docker.io's IP ranges only.
# (Resolve registry-1.docker.io's current A records; the exact CIDRs
# change as the registry CDN updates its IPs. Use iptables with ipset
# for maintainability.)

sudo iptables -A OUTPUT -p tcp --dport 443 -d <registry-1.docker.io-CIDR> -j ACCEPT
sudo iptables -A OUTPUT -p tcp --dport 443 -d <auth.docker.io-CIDR> -j ACCEPT
sudo iptables -A OUTPUT -p tcp --dport 443 -j DROP
# Note: also allow outbound to DEV's publication directory (rsync),
# health check endpoints, and any internal services.
```

The firewall is the authoritative control; the daemon config is defense-in-depth.

### 4.3 Verification

From prod:

```bash
# Verify a non-allowlisted pull is blocked.
docker pull alpine:latest
# Expected: network error / registry unreachable.

# Verify an allowlisted pull succeeds.
docker pull itzg/minecraft-server@sha256:<64-hex>
# Expected: successful pull.

# Verify the pulled image hashes to the digest we expect.
docker image inspect --format '{{index .RepoDigests 0}}' itzg/minecraft-server@sha256:<64-hex>
# Expected: itzg/minecraft-server@sha256:<64-hex>  (matches)
```

---

## 5. Deploy-time verification

The deployment daemon (`daemon/deploy.py`) does not currently re-verify image digests at deploy time; it relies on:

1. The slip's `image_digest` being correct (Phase 3 verification above).
2. Prod's docker daemon only being able to pull the pinned digest (Phase 4 above).

If the digest is correct in the slip AND the daemon can only pull that digest, a supply-chain substitution between slip-promotion and prod-deploy is impossible. This is the current model.

**Future enhancement:** §12.4 stage 18 (health checks) could additionally verify `docker image inspect` for the running container against `slip.runtime.image_digest` as an explicit assertion. Not currently implemented; noted here so future contributors know the gap exists.

---

## 6. Rotation and updates

### 6.1 Routine image updates

When a new `itzg/minecraft-server` image is published:

1. DEV: `docker pull itzg/minecraft-server:<new-tag>` (or `:latest`).
2. DEV: resolve the new digest per §3.1.
3. DEV: run `materialize` (the digest is captured automatically). **Do not promote the slip until §3.3 verification passes.**
4. DEV: `iterate` to boot-test the new image.
5. DEV: approve via Discord, `promote`, `publish`, per the normal release flow.
6. Prod pulls the new release bundle, sees the new `image_digest`, and `docker pull`s it. The firewall allows it because the digest resolves to an allowed registry.

### 6.2 Registry allowlist additions

See §2. The procedure requires:

- Written justification in a PR to this document.
- Trust posture assessment (does the registry sign images? is it mirrored? what's the TLS pinning story?).
- Firewall rule update on prod.
- Testing on a prod clone before deploying to prod.

---

## 7. Sign-off checklist

- [ ] `/etc/docker/daemon.json` (or firewall rules) enforce the allowlist per §4.
- [ ] A non-allowlisted pull is blocked (§4.3).
- [ ] An allowlisted pull succeeds (§4.3).
- [ ] `slip.runtime.image_digest` in the current `stable` slip is a real digest, not the placeholder (§3.3).
- [ ] `docker pull` by that digest succeeds on prod.
- [ ] The pulled image's `RepoDigests` matches the slip's `image_digest`.

**Signed off by:** ____________________ **Date:** ____________

---

## 8. Appendix — current pinned images

Maintained by the operator. Update on every image change.

| Environment | Image reference | Digest | Pinned in slip | Deployed |
|---|---|---|---|---|
| prod | `itzg/minecraft-server:java25` | `sha256:<pending>` | v1.0.0 (baseline) | pending |
| DEV | `itzg/minecraft-server:java25` | `sha256:<pending>` | latest-slip | — |

---

**End of OCI Registry Allowlist.**
