# Verifying a release

Every release from 0.7.0 on can be checked before you run it: the container image is signed with [cosign](https://docs.sigstore.dev/cosign/) without a long-lived key, the image and the push-agent binaries carry GitHub build provenance, and the release lists what is inside each part in CycloneDX SBOMs. This page shows how to check each of them. Releases before 0.7.0 have only the checksums and the Ed25519 signature described under "Push-agent binaries".

The examples use version `0.7.0`; put the version you are installing in its place.

## What a release contains

| Asset | What it is | How to check it |
|---|---|---|
| `ghcr.io/quaresma870/perchtail:<version>` | The container image (amd64, arm64) | cosign signature and build provenance |
| `perchtail-image.json`, `perchtail-image.json.sig` | The image's digest, signed with the release key | Ed25519 signature |
| `perchtail-agent-<version>-<os>-<arch>` | The push-agent binaries | build provenance, `SHA256SUMS` and its Ed25519 signature |
| `SHA256SUMS`, `SHA256SUMS.sig` | Checksums of the agent binaries, signed with the release key | Ed25519 signature |
| `perchtail-<version>.sbom.cyclonedx.json` | SBOM of the image | |
| `perchtail-backend-…`, `perchtail-frontend-…`, `perchtail-agent-<version>.sbom.cyclonedx.json` | SBOMs of the backend, frontend and agent dependencies | |

## The container image

### Signature (cosign)

The release workflow signs the image by digest with a short-lived certificate that GitHub issues to that workflow run, and records the signature in Sigstore's public transparency log. Verifying it proves the image was built by this repository's release workflow, from a release tag or from the `dev`, `main` or a `hotfix/` branch:

```bash
cosign verify ghcr.io/quaresma870/perchtail:0.7.0 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/quaresma870/perchtail/\.github/workflows/release\.yml@refs/(tags/v[0-9].*|heads/(dev|main|hotfix/.+))$'
```

cosign prints the verified signatures and exits with 0. Any other result means the image isn't the one the release workflow built: don't run it.

To run exactly what you verified, use the digest instead of the tag, for example in `docker-compose.yml`:

```yaml
image: ghcr.io/quaresma870/perchtail@sha256:<digest>
```

The digest is in `perchtail-image.json` and in cosign's output.

### Build provenance

The image also carries a GitHub build provenance attestation, which records the repository, the commit and the workflow that built it. Check it with the [GitHub CLI](https://cli.github.com/):

```bash
gh attestation verify oci://ghcr.io/quaresma870/perchtail:0.7.0 --repo quaresma870/perchtail
```

### The signed image manifest

`perchtail-image.json` names the release's image by digest, and `perchtail-image.json.sig` is its Ed25519 signature with the release key, the same key that signs the agents' checksums. To check it, get the public key from the release's tag and verify with OpenSSL (3.0 or later):

```bash
curl -fsSLO https://raw.githubusercontent.com/quaresma870/perchtail/v0.7.0/agent/internal/agent/upgrade_signing_key.pub
openssl pkeyutl -verify -pubin -inkey upgrade_signing_key.pub -rawin \
  -in perchtail-image.json -sigfile perchtail-image.json.sig
```

## Push-agent binaries

Check the checksums, then the signature over them, then (optionally) the provenance of the binary you use:

```bash
sha256sum --ignore-missing -c SHA256SUMS
openssl pkeyutl -verify -pubin -inkey upgrade_signing_key.pub -rawin \
  -in SHA256SUMS -sigfile SHA256SUMS.sig
gh attestation verify perchtail-agent-0.7.0-linux-amd64 --repo quaresma870/perchtail
```

The Ed25519 signature is the one agents check themselves before they install an upgrade from the portal, against the public key built into them.

## SBOMs

The SBOMs are CycloneDX JSON files attached to each release. Feed them to the tool you already use to track vulnerable dependencies (for example `trivy sbom`, `grype sbom:`, or Dependency-Track) to see whether a newly published vulnerability affects the version you run:

```bash
trivy sbom perchtail-0.7.0.sbom.cyclonedx.json
```

## Building from source

The backend's `requirements.txt` pins every package with its hashes, and the Docker image installs it with `pip --require-hashes`, so a package that doesn't match what was pinned fails the build. A from-source install can do the same (see `docs/install-from-source.md`).
