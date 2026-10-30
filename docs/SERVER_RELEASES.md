# Controlled server releases

Normal startup follows `server-release.json` in this repository. That file names
one exact upstream commit, workflow run, artifact, archive digest, executable
digest and project release. It is a maintainer decision, not an upstream commit
subscription. The project release stores the original verified ZIP and the
corresponding upstream source archive. It survives upstream artifact expiry.

The runtime stable channel downloads only the project release URL. A missing
release, bad manifest, bad digest, incompatible manager version or network error
keeps the installed server. It never tries upstream's latest build as a fallback.
The separate CI preparation tool may bootstrap an unpublished pin from its exact
upstream workflow, after verifying the workflow and artifact metadata.

## Choosing an update channel

`./EpinelPS --channel stable` remembers the normal project-controlled channel.
`./EpinelPS --channel experimental` explicitly remembers upstream's latest
successful push build as the source instead. `--offline` skips both checks for one
launch, and `auto_update: false` continues to disable automatic updates.

The stable channel never automatically replaces a known newer workflow build.
When an installed version has no workflow metadata, it is kept unless its
executable exactly matches the pin. To intentionally adopt the stable version,
close the game and server and run `./EpinelPS --adopt-stable`. This remembers the
stable channel and checks the pin even if automatic updates were disabled; it
does not re-enable automatic updates. Startup snapshots, certificate validation,
client trust verification and failed-start rollback still apply. An intentional
switch to older code can have gameplay or schema compatibility consequences that
startup checks cannot fully detect; keep the existing installation and backups.

An accepted release sequence cannot silently move backward or change its build
identity. A previously used server may be selected again by a new, higher release
sequence after its compatibility has been reviewed; installations with newer
saves still need an explicit adoption before moving to older code. The accepted sequence is
saved only after live startup and client-bundle TLS checks pass.

## Promoting a candidate

1. Review an exact upstream commit and its successful main-branch push workflow.
   Obtain the Linux artifact ID, byte size and SHA-256 from GitHub. Download those
   exact bytes, verify their digest, and compute the SHA-256 of the extracted
   `EpinelPS` executable. Keep the archive's original bytes for publication.
2. Update `server-release.json`: increment `sequence`, choose a new unique
   `server-YYYY.MM.DD.N` release name, record all build metadata and set the minimum
   manager version. Do not reuse or rewrite a published release identity.
3. Run `python3 tools/prepare_server.py --output /tmp/epinel-candidate`. The output
   directory must be new. This verifies the origin when bootstrapping, checks the
   archive and executable digests, safely extracts the server and verifies strict
   certificate repair. It never accesses the user's installed server or saves.
4. Run the complete suite with `EPINEL_TEST_SERVER` set to the prepared
   `server/EpinelPS`. Test actual client login and relevant gameplay separately
   before claiming those features are supported. Record the tested scope honestly.
5. Commit the reviewed pin. GitHub runs regressions on Python 3.10 and 3.14, then
   all tests including native database migrations on Python 3.12. Publication is
   available only on this repository's main branch after these jobs pass.

The publishing job creates a draft server release containing the unchanged ZIP,
the corresponding source tarball and license, the pin and `SHA256SUMS`; it then
publishes the complete release. A pin whose release is not yet published cannot
download a new stable server. Existing published assets are never overwritten.
An interrupted draft publication needs maintainer inspection before retrying.

The same successful workflow publishes the manager's source package under its
own `vX.Y.Z` tag when that version does not exist. Bump `__version__` for a new
manager package; a server approval and a manager update are separate decisions.
The manager's application updater preserves `.epinel` and does not re-import
saves. It copies the bundled pin and this documentation with the application.

This controls deployment and retention of upstream builds. It does not maintain
a separate fork of the server's source or automatically fix gameplay regressions.
