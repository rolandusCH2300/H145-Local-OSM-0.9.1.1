# Security

## Supported versions

Security fixes are provided for the latest published release. Update to the newest release before reporting an issue that may already be fixed.

## Reporting a vulnerability

Do not open a public issue for an undisclosed vulnerability.

Use GitHub private vulnerability reporting or a private security advisory when available. Include the affected version, reproduction steps, impact, and relevant logs or proof-of-concept input.

## Security model

H145 Local OSM is a local desktop service.

- The HTTP server listens only on `127.0.0.1:8765`.
- The local server does not use authentication or TLS because it is restricted to loopback.
- Runtime mission queries are served from the selected local SQLite database and do not fall back to a public Overpass service.
- Outbound access is used for Geofabrik metadata and OSM downloads.
- HPG and R&E compatibility patches create backups before modifying supported installed files.
- Runtime database readers use query-only/read-only access where supported.

Do not expose the listener on another interface without adding appropriate authentication, TLS, access control, and input limits. Keep the extracted application directory intact and do not replace files in `_internal` with untrusted binaries or libraries.

## Release safeguards

Official Windows releases are produced by GitHub Actions and are gated by:

- pinned and audited Python release dependencies;
- the application unit-test suite;
- packaged runtime, GUI, and multiprocessing checks;
- Microsoft Defender scans; and
- SHA-256 manifests, dependency/version records, and GitHub artifact attestation when supported.

A release is published only after the required build and verification jobs succeed.

## Verifying a release

Official releases include `SHA256SUMS.txt`. On Windows PowerShell:

```powershell
Get-FileHash .\H145-Local-OSM-portable.zip -Algorithm SHA256
```

Compare the result with the matching entry in `SHA256SUMS.txt` from the same release.

When an artifact attestation is available:

```powershell
gh attestation verify .\H145-Local-OSM-portable.zip -R ratuom/H145-Local-OSM
```

Hashes and attestations verify integrity and provenance; they do not replace endpoint security controls or malware scanning.
