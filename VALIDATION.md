# Validation history

## Controlled server releases — 2026-10-30

EpinelPS Managed **0.2.0**, Linux x86-64.

Normal startup resolves the project-owned stable pin and downloads only the
project's archived release. Upstream main is queried only after an explicit
experimental-channel selection. Existing settings, paths, data and disabled
update preferences survive the policy change.

The pinned upstream build is `e31e66386bba0128bacca9634205e444bf408041`, workflow
`40219500183`, artifact `12609476148`, size **67,236,646 bytes**. Its original ZIP
SHA-256 is `8b62e6b5a472e6845a215692a106843ac40bc314f9e0c602e2517f09bd3c77b6`.
The executable SHA-256 is
`ce6c778c4668d9595d5830f2124006f9410e8cc8c0e308ae79bc17f6b8833215`.
GitHub's workflow/artifact metadata and the actual downloaded bytes were checked.
The existing strict-CA repair successfully repaired that actual upstream pair.

Verification for this change:

- **91 automated tests**, including four native migration tests against this exact
  upstream executable. Native tests use isolated fixture saves and databases.
- New tests cover project-only stable resolution, explicit experimental selection,
  malformed/incompatible pins, missing release downloads without upstream fallback,
  preservation of newer or unknown installations, explicit stable adoption,
  monotonic accepted releases, executable integrity, channel persistence without
  setup prompts, approval only after successful startup and rollback on failure.
- Publication bootstrap checks reject wrong workflow commits, pull-request builds,
  failed runs, expired artifacts, changed sizes and wrong archive digests.
- Existing tests still cover download recovery, strict TLS, both client trust stores,
  single-folder discovery, port conflicts, SQLite WAL snapshots, application update
  rollback and preservation of gameplay progress after accepted startup.
- The application updater copies the bundled pin while preserving saved settings
  and data. The startup helper binary remains unchanged.
- Python, shell and workflow syntax were checked. GitHub CI runs Python 3.10/3.14
  regressions and a Python 3.12 run with all native tests enabled. Publishing the
  approved server and manager package requires these checks to pass on main.

The server release archives the unchanged verified upstream ZIP, its corresponding
source archive and license, the pin and SHA-256 sums. Runtime stable downloads do
not depend on upstream's temporary Actions artifact remaining available. New
server pins require a separate maintainer decision and a new release sequence.

No actual client login or gameplay was run here. Native checks verify database
migration compatibility; local TLS fixtures verify startup/connection handling.
Choosing a stable pin is not evidence that every gameplay feature works.

## Previous 0.1.4 validation — 2026-10-26

EpinelPS Managed 0.1.4, Linux x86-64.

## Regression being fixed

The supplied v0.1 log reported `SQLite Error 1: no such table: Triggers` during
saved-user reconciliation, before ports or TLS were reached. v0.1 omitted the
executable-side `epinelps.db` alias even though upstream's early context creation
uses that path before loading configuration. Upstream also reconciles saved
users before initializing its database.

Version 0.1.1 adds that alias, archives detached v0.1 database files, initializes
the canonical SQLite context and upstream migrations before Main through a
bundled startup helper, and retries v0.1-rejected commits once. No SQL schema is
invented by the Python launcher: initialization uses the selected binary's own
`DbInitializer.Initialize` method. All initialization runs inside the existing
save-snapshot/failed-start recovery boundary.

## Checks

- **70 automated tests passed**, including four tests against the real upstream
  Linux .NET executable and its actual Entity Framework migrations.
- Existing tests cover interrupted/resumed downloads, checksums, archive
  validation, certificate expiry/mismatch, game/launcher trust, save snapshots,
  failed-start fallback, interrupted-launch recovery, and post-acceptance saves.
- New tests cover the executable-side database path, preservation of detached
  databases/WAL files, one-time retry of old rejected commits, verified helper
  injection, and preventing path changes while a server is running.
- Real native tests create the schema from a JSON-only saved user with ID
  `188898165`, preserve its stage progress, import its trigger, and verify that
  rerunning initialization does not duplicate users or triggers. Existing
  unrelated database rows survive. A corrupt database fails before the JSON save
  is changed. The helper completes before the actual upstream Main executes.
- Application update tests verify that saved data and state are untouched,
  a running manager blocks updates, and failed application writes restore the
  previous application files.
- The startup helper built with .NET SDK 10.0.402 targeting net10.0 with zero
  warnings/errors. No SDK or separate runtime is required on the user's system;
  the helper runs inside the upstream server's self-contained runtime.
- Python source compilation and shell syntax checks passed.

Upstream tested:

- Commit: `e31e66386bba0128bacca9634205e444bf408041`
- Successful push workflow: `40219500183`
- Linux artifact: `12609476148`
- Artifact SHA-256:
  `8b62e6b5a472e6845a215692a106843ac40bc314f9e0c602e2517f09bd3c77b6`
- Startup helper SHA-256:
  `0dc5b456e7c146defb768ae0145c2040be3b2c1b29167a64e7b3435381ba91b9`

Native checks run with `EPINEL_TEST_SERVER` pointing to a downloaded upstream
Linux server executable. They use temporary fixture saves and do not access the
user's actual saved game. Process identity tests use kernel-data fixtures where
this container's mounted `/proc` differs from the subprocess PID namespace.

Actual NIKKE login/gameplay, graphical polkit dialogs, and the user's installed
game assets have not been tested here. Local HTTP/TLS fixtures cover supervisor
acceptance and rollback; native tests cover the actual database code path.

## Client connection regression in 0.1.2

The supplied debug showed database initialization succeeding and the server
listening on 80/443, without the manager's final Ready line. The screenshot
showed a Windows launcher/ServerSelector certificate kit inside a Bottles prefix,
outside v0.1's common-directory scan. The actual certificate contents in that
installation were not supplied; their file dates alone do not establish validity.

Version 0.1.2 requests explicit pasted locations and re-confirms paths selected by
older releases. It repairs the selected game's SDK bundles, the chosen launcher's
SDK bundles, and the public CA files used by Windows ServerSelector. A public-only
myCA.pfx is cached so it is both stable across runs and usable by old selectors.
Local SDK/lobby hosts routes are maintained transactionally for the system and the
selected Wine/Bottles prefix. Asset CDN routing is preserved.

New tests cover copied paths, quoted paths and file URLs; Bottles prefix inference;
explicit setup without a game scan; cancellation preserving saved settings;
launcher bundle/PFX synchronization; local hosts repair preserving unrelated names
and comments; idempotence; live rejection of a wrong client CA; and health checks
that finish after a complete HTTP body without waiting for TLS connection close.
The latter reproduces a failure mode of the old raw-socket health check.

A separate .NET 10 probe loaded the generated public-only PFX using the same
X509Certificate.CreateFromCertFile / X509Certificate2 API used by Windows
ServerSelector. It loaded successfully and reported HasPrivateKey=False.

The automated tests use local HTTP/TLS fixtures and temporary selected game paths.
The four real upstream executable migration tests still pass. Actual NIKKE
login/gameplay and graphical polkit prompts remain untested in this environment.

## One-folder setup and port conflicts in 0.1.3

The new supplied log shows `https://0.0.0.0:443: address already in use` for both
the updated and previous binaries. The user confirmed that another server was
still running after closing the laptop lid, then reported successful operation
after resolving that conflict. This was a port conflict, not evidence of another
certificate failure.

The old preflight returned early when binding port 80 needed permission, which
could skip the occupied port 443. The new check inspects IPv4/IPv6 listeners and
checks every port before requesting capabilities. It runs before downloads and
before executable-side database/certificate changes. A conflict does not reject
the candidate build or start a fallback. A conflict arising during child startup
also stops the attempt and preserves the build's eligibility.

Setup now asks for one install folder and discovers its game, companion launchers,
and selector within that tree. Discovery skips asset directories, has depth/work
bounds, and does not follow nested directory symlinks into another installation.
Working explicit paths from 0.1.2 are kept without another prompt. A cancelled
selection preserves settings; external launcher locations remain supported.

New regressions cover the single prompt, remembered settings on the next run and
on upgrade, nested layouts, multiple-game ambiguity, scan boundaries, a real
occupied TCP socket, IPv4/IPv6 listener parsing, port-80 permission denial followed
by occupied port 443, and conflicts before and during startup. Existing download,
certificate, save-recovery, live-TLS and real upstream migration tests still pass.

## Strict CA verification and recovery in 0.1.4

The PC log showed upstream commit `e31e66386bba0128bacca9634205e444bf408041`
listening successfully, followed by `CA cert does not include key usage extension`.
The follow-up log confirmed automatic recovery: it restored the pre-update saves,
started imported commit `3c8b7811ddfbe1ddd489a992f87525e421631433`, issued a
local certificate, verified the real game/launcher trust bundles, and reached Ready.
That recovery left the newer build rejected; it did not repair that build's CA.

The same upstream CA/PFX pair was reproduced locally: ordinary OpenSSL verification
passed; `openssl verify -x509_strict` failed with error 92, missing CA key usage.
The preflight now uses strict verification consistently. An invalid upstream or
cached pair triggers local certificate issuance before child startup. Live probes
also enable strict verification on Python versions whose defaults are less strict.
Certificate validity and hostname verification remain enforced.

New synthetic-certificate tests reproduce a matching, unexpired pair with no CA
key-usage extension, verify that preflight rejects and repairs it, confirm that the
repaired local CA is retained, and exercise the same error through a live TLS
connection. State-upgrade tests retry affected rejected builds once while keeping
unrelated rejected builds. Discovery tests exclude `Launcher/log/launcher`, even
if an earlier release placed public CA files in that log directory.

The test runtime is Python 3.12.14 with OpenSSL 3.0.13, with strict mode explicitly
enabled. This reproduces the relevant validation behavior without depending on
the PC's unreported Python/OpenSSL versions. Python's documentation notes that
strict verification became a default of `create_default_context` in Python 3.13:
[Python SSL documentation](https://docs.python.org/3/library/ssl.html).

No actual NIKKE login/gameplay was run in this environment. The user's PC reported
Ready on the fallback build; the updated manager's repair path is verified with
synthetic certificates and the actual pinned upstream certificate pair.
