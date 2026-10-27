# EpinelPS Managed 0.1.4 — Linux x86-64

A community-maintained Linux launcher for
[EpinelPS](https://github.com/EpinelPS/EpinelPS) local-server installations.
It downloads upstream's compiled server, verifies the exact build, checks its
certificate chain, repairs the game and Linux launcher's trust, and starts the
server. Upstream updates cannot replace the maintenance launcher.

> **Independent project.** EpinelPS Managed is not affiliated with, sponsored by,
> or endorsed by any game developer or publisher, or by the upstream EpinelPS
> maintainers. It is a separate maintenance project, not an official upstream
> release.

**Rights notice:** This software license covers the rights granted by its
contributors. It does not grant rights to third-party proprietary content or
establish compliance with third-party terms. Read
[Project scope and third-party rights](#project-scope-and-third-party-rights)
before using or publishing it.

**First-time Wine/Bottles users:** Complete the
[client setup in the game's own prefix](#first-time-winebottles-setup) before
starting the manager. The maintenance launcher imports an existing local-server
installation; it does not install or run the upstream server switcher for you.

## Updating an existing installation

Extract this update into a separate folder and open **`Apply-Update.sh`**. It finds
`~/EpinelPS-Managed` automatically, or opens a folder picker for your existing
managed installation. Close any running managed server first.

The updater replaces application files, preserves `.epinel` saves/settings/cache,
keeps a backup of replaced application files, and starts the installed copy.
There is no re-import and no need to edit state files or run `setcap` manually.

Version 0.1.4 fixes a certificate compatibility failure exposed by stricter TLS
verification on some PCs: a matching, unexpired upstream CA can still lack a
required X.509 key-usage extension. The manager now checks strict certificate
structure before starting the server and repairs invalid pairs with its persistent
local CA. Live checks use strict verification on older Python versions too.
Previously rejected builds with invalid cached certificate pairs are retried once
after this update; unrelated rejections remain remembered. Launcher discovery also
skips log folders such as `Launcher/log/launcher`.

Setup asks for **one game installation folder**. Use **Copy Location** in Dolphin
(or another file manager) and paste it into the prompt. The tool finds the game,
companion launcher, and server selector inside that installation and remembers
them. There is no separate launcher-location prompt. An installation root, the
game folder containing its executable, or the executable itself are accepted,
including quoted paths and `file:///` URLs. Working paths saved by version 0.1.2
are reused without another location prompt.

For Bottles, copying the installation root under
`~/Bottles/prefixes/<bottle-id>/drive_c/` is supported. Setup derives its game,
companion launcher, Windows selector certificate kit, and Wine prefix from that
selected location. Discovery is bounded to that installation, skips asset trees,
and does not follow directory links into other installations. Nonstandard nested
layouts are supported; if a broad folder contains multiple games, select the
specific installation. Extra launcher locations previously selected explicitly
are preserved, and `--launcher` remains available for an external launcher.
If you already merged the archive into the installed folder, use the same script
or start normally.

The database fix from version 0.1.1 repairs the missing `epinelps.db` link used by Epinel's early startup
code. Detached databases left beside v0.1 executables are archived rather than
silently deleted. A bundled startup helper then runs **the migrations and legacy
account import from the selected upstream binary** before its saved-user
reconciliation. This creates/upgrades `Users`, `SdkUsers`, and `Triggers` using
upstream's schema, including JSON-only imports. Save snapshots cover these
operations. Native server startup is accepted only after that helper confirms
completion. Commits rejected by v0.1 are retried once after installing the fix.

## First-time Wine/Bottles setup

For the Windows launcher workflow, use upstream's **ServerSelector**
(`ServerSelector.Desktop.exe`) inside the **same Wine prefix or Bottles bottle
that contains the game and its launcher**. This is a first-time client setup step.
The selector applies upstream's client-library changes as well as initial
certificate and routing configuration. This manager maintains certificates and
local routing afterward, but does not apply the selector's client DLL changes.

1. Close the game, its launcher, and any running local server.
2. Obtain the complete selector package from the
   [upstream Windows server/selector build](https://nightly.link/EpinelPS/EpinelPS/workflows/dotnet-desktop/main/Server%20and%20Server%20selector.zip).
   Keep the executable, its support files, `sodium.dll`, and certificate files
   together. A convenient location is the game's installation root inside that
   prefix, for example `<prefix>/drive_c/<game-install-folder>/`.
3. In Bottles, select the game's existing bottle and use its option to run an
   executable; choose `ServerSelector.Desktop.exe`. With another Wine frontend,
   explicitly use the game's existing prefix. Running it in the correct prefix
   matters because its Windows certificate-store and system-file changes belong
   to that environment.
4. In the selector, choose the actual game installation root: the folder
   containing the companion `Launcher` folder and the game's folder tree. Select
   **Local server**, use **`127.0.0.1`** for a server on this same PC, and save the
   configuration. Then close the selector.
5. Continue with the managed-server steps below. The manager discovers this same
   prefix from the selected game path and maintains its SDK trust bundles,
   selector CA files, and local hosts routes on normal startup.

The Linux server and this manager run natively on Linux. The Windows game,
launcher, and Windows selector use the game's Wine/Bottles prefix. Placing the
selector files inside the prefix is convenient; **launching it in the game's
actual prefix is the important requirement**.

If the game already connects to a local server through a configured prefix, keep
that setup and start the manager normally. You do not need to rerun the selector
for every server start or server update. Game-client updates or repairs may
restore original client files; follow upstream's switching instructions before
updating the client and reapply local-server setup afterward when required.

Users of the separate native Linux launcher should follow that project's
first-time setup instructions instead. A Windows selector is not an additional
requirement for an installation already configured through that supported workflow.

## Start here

1. Extract this archive into its **own permanent folder**, next to your existing
   EpinelPS installation. Do not extract it over the original server executable.
2. Complete the [first-time Wine/Bottles setup](#first-time-winebottles-setup) if
   needed, then close the original EpinelPS server, the game, and its launcher for
   the first import.
3. Open `Start-EpinelPS.sh`, or run `./EpinelPS` from this folder.
4. Select the original server if asked. Paste **one game install-folder location**
   copied from your file manager. Its game and launcher are discovered together.

After setup, use the same `./EpinelPS` or start script every time. The original
server folder stays intact. The managed copy owns its imported saves from that
point forward: use this entry point consistently to avoid playing on two diverging
copies of your save.

Optional: open `Install-Desktop-Shortcut.sh` once to add EpinelPS Managed to your
application menu. The entry also has setup and certificate-repair actions.

Python **3.10 or later** and OpenSSL **3.x** are required; no Python packages, Git,
.NET SDK, or compilation are needed. KDE uses KDialog for location entry; Zenity is an alternative. A terminal paste
prompt is the fallback. Server/import/update folder selection can also use Tk.

## What happens at startup

- Check that ports 80/443 are available before downloading or activating an
  update. An earlier server can keep running after a laptop suspends and resumes.
  Occupied ports produce a clear message, including the process name/PID when
  available; they do not mark a healthy upstream build as broken or trigger a
  fallback. Close the other server normally and start again.
- Check successful **push builds from upstream main**, excluding pull-request
  builds and unrelated workflows. The update is a precompiled Linux build, not a
  source checkout that requires building on your laptop.
- Resolve an immutable workflow run and artifact. Download with streaming
  progress, a 30-second socket inactivity limit, and up to four attempts. Partial
  downloads resume when the server supports HTTP ranges; otherwise they restart
  cleanly. There is no 100-second whole-download limit.
- Verify the full archive against **GitHub's SHA-256 artifact digest**. Reject
  missing digests, wrong sizes, malformed packages, traversal paths and symlinks.
- Fetch the public CA from **that same upstream commit**. Validate strict X.509
  structure (including CA key usage), dates, chain, required hostnames, server
  usage, and private-key correspondence.
- If upstream ships an invalid/expiring pair, issue a replacement from a private
  local CA. Preserve that CA between starts and renew certificates automatically.
- Run the selected server's own database migrations and legacy account import
  before its normal startup code, using the bundled .NET startup helper. No .NET
  SDK or separate runtime installation is needed. Then start with upstream's
  interactive updater disabled and verify live TLS and HTTP before acceptance.
- Repair both SDK bundle names when present: `intl_cacert.pem` and `cacert.pem`
  inside the game client's `Plugins/x86_64` directory, plus the launcher's `intl_service`
  bundles when present. Update `myCA.pem`/`myCA.cer` in selected EpinelPS Linux
  launcher and server-selector folders. A public-only `myCA.pfx` is also generated
  so older Windows selectors inside Wine cannot reinstall a different CA later.
  Repeated launches do not append duplicates.
- Preserve unrelated vendor certificates, headers and trailing bundle content.
  Maintenance downloads and live server checks validate TLS and hostnames. Private CA keys are
  never copied into the game or launcher.

The game must be closed when its trust files need to change. When paths move,
setup asks for the new installation folder. The desktop setup action changes
that folder; optional `--launcher` arguments add external launcher locations.

After you confirm the game path, the manager repairs local SDK/lobby routes in
`/etc/hosts` and the selected Wine/Bottles prefix's hosts file. Other hosts and
aliases are preserved; game asset CDN traffic is not redirected by this feature.
A normal system authentication dialog appears only if a protected file needs to
change. The route block remains compatible with upstream ServerSelector's
Official-server switch. Starting the managed local server reinstates local routes.

The manager checks the live server using the actual selected game's and launcher's
certificate bundles for lobby and login SDK hostnames, rather than only trusting
its own CA file. It prints every checked bundle path and writes
`.epinel/connection-report.txt`, including an explicit reason if verification fails.
An upstream Windows launcher inside Bottles is supported; a native Linux launcher
is not required. Certificate timestamps alone are not treated as proof of validity.

Initial game installation, Wine creation, and game DLL patching remain with your
existing game setup. Complete the
[upstream client setup in the correct prefix](#first-time-winebottles-setup), or
use an already configured installation. This manager does not install or invoke
the upstream selector automatically.
No system-wide Linux root CA is installed, and the CA private key is never copied
to a game or launcher. Public selector PFX files contain no private key.

## Saves, recovery and permissions

The original server is imported without overwriting it. Both `db.json` and the
SQLite database are copied, including committed WAL data through SQLite's backup
API. Cached assets use hard links where supported, with copying as a fallback.

Managed saves, settings, assets and logs live under `.epinel/data/`. Code versions
live separately under `.epinel/versions/`. `appsettings.json` is preserved with
its SQLite path pointed at the managed data directory. The legacy executable-side
`epinelps.db` path links to that same database; it cannot create a second save store. Upstream game version,
asset URLs and salts refresh together; `EnablePurchaseSimulation` and
`ArchiveUnlockAll` preferences carry forward. Custom game-resource overrides
should be reviewed after an update.

Before startup, a consistent save snapshot is stored under `.epinel/backups/`.
If a candidate fails startup, the launcher stops it, restores both save stores
and any partially changed trust files, then tries the previous installed build.
Failed candidate commits are remembered until a newer build arrives, or you
explicitly retry them. An interrupted unaccepted startup is recovered on the
next launch. **After a server is accepted, later crashes never automatically
restore an older save and discard subsequent game progress.**

For ports 80/443 on systems that restrict low ports, the launcher requests the
`cap_net_bind_service` capability for the server executable using the normal
polkit password dialog. It reapplies this to new binaries as needed; it does not
run the whole server as root. This requires distribution `setcap`/`getcap` tools
and a working polkit agent. Protected game trust files likewise use a normal
authentication dialog when a repair requires it.

Upstream updates are automatic by default. A successful build/startup does not
prove that every game feature works: the check verifies local server startup and
TLS, not actual game login or gameplay. Preserve your original installation as
an additional recovery option.

## Optional controls

Normal startup requires no arguments. These are optional power-user controls:

| Command | Purpose |
| --- | --- |
| `./EpinelPS --offline` | Start the installed build without checking for updates. |
| `./EpinelPS --status` | Show the installed version and remembered paths. |
| `./EpinelPS --setup` | Paste one installation folder again, discover its components, then start. |
| `./EpinelPS --repair` | Check and repair certificates without starting the server. |
| `./EpinelPS --retry-update` | Retry the latest build after a previous startup rejection. |
| `./EpinelPS --import /path/to/server` | Explicitly choose the original server on first setup. |
| `./EpinelPS --game /path/to/game --launcher /path/to/launcher` | Explicitly remember paths; `--launcher` may be repeated. |

Maintenance settings are in `.epinel/settings.json`; `auto_update` can be set to
`false`, and `startup_timeout` defaults to 900 seconds to allow asset preparation.
Startup logs are in `.epinel/manager.log`; connection checks are in `.epinel/connection-report.txt`; upstream logs are in `.epinel/data/logs`.
Version archives and snapshots are retained in this first release; keep an eye on
disk space if you update frequently.

Managed automatic database recovery currently supports SQLite. Installations
using external SQL servers need a separate backup integration before migration.
Windows packaging is not included in this Linux release.

## Development and verification

The launcher uses only Python's standard library plus the distribution's OpenSSL
binary. Run `python3 -m unittest discover -s tests -v` from this folder.

Tests use local HTTP/TLS servers and real OpenSSL certificates. They cover dropped
connections, range resumption, unknown download lengths, digest failures, archive
validation, expired/mismatched certificates, idempotent game bundle repair,
transactional trust rollback, consistent WAL backups, import preservation,
interrupted-start recovery, failed-update fallback, and preservation of progress
after a successful startup.

The original production updater and certificate validation were checked against
upstream commit `9eeb666d48068703b9212b4ac8bb4fd2572bb179`, workflow run
`40155729847`, Linux artifact `12584188885`, SHA-256
`c1eb81c13ecfbfc5ae4d6911362705e7a965e492d12db1a8f857ce3425606d17`.
Version 0.1.1 also runs native migration tests against upstream commit
`e31e66386bba0128bacca9634205e444bf408041` (workflow `40219500183`,
artifact `12609476148`). To run them, set `EPINEL_TEST_SERVER` to a downloaded
upstream Linux executable while running the test suite; tests use temporary
copies and fixture saves, never the real installation's data. They cover real
schema creation, existing-user/trigger preservation, migration idempotence,
corrupt-database failure, and initialization before the native entry point.

The compiled startup helper, its integrity manifest, and its complete C# source
are included. Developers can rebuild it with .NET SDK 10 and
`tools/build_startup_hook.sh`. Ordinary startup needs no build tools.

Version 0.1.2 adds copied-location handling, Bottles prefix routing, Windows
selector public-PFX synchronization, and live TLS tests using client trust stores.
Its HTTP health check parses response framing and does not wait for TLS EOF after
a complete HTTP body. Listening servers that fail the probe produce diagnostic
messages instead of a silent fifteen-minute wait.

Version 0.1.3 uses one installation-location prompt, discovers its game and
launchers together, and reuses working 0.1.2 paths without prompting. Port checks
cover both 80 and 443 even when low-port binding needs permission, include IPv6
listeners, and separate port conflicts from failed upstream builds.

Version 0.1.4 adds regression tests for the missing-CA-key-usage failure: an
otherwise matching, valid-date certificate pair passes ordinary OpenSSL checks
but is rejected by strict verification. Tests cover automatic repair, stable
local-CA reuse, live strict-TLS rejection, one-time retry of affected builds, and
exclusion of launcher log folders. See [VALIDATION.md](VALIDATION.md) for results.

Actual client login/gameplay has not been tested in this environment.

## Project scope and third-party rights

This repository provides the maintenance code, shell scripts, tests,
documentation, and a startup helper with its complete C# source. The packaged
helper is built from that included source. The repository and maintenance
release archive do **not** include a proprietary game client, artwork, music, story,
game data packs, proprietary server code, game DLL patches, or private
certificate keys. Obtain any third-party software through an authorized source
and use it only where you have the necessary rights.

At runtime, the tool imports an existing installation selected by the user,
including its local saves and cached files, and downloads compiled EpinelPS builds
from upstream. The upstream server may retrieve additional game resources during
operation. Those downloaded or imported materials are not relicensed by this
project. A public download URL or an upstream open-source license does not itself
establish permission to use or redistribute third-party game content.

The tool also changes local game/launcher certificate bundles and SDK/lobby hosts
routes so the selected installation can connect to the local server. These are
real client and routing changes, described in the startup section. This README does not represent
them as approved by the game's developer or publisher.

Use the software only where permitted by applicable law and any agreements that
apply to you. Third-party terms may restrict client modification, server
emulation, communication redirection, or reverse engineering. This README does
not grant permission for those activities or provide legal clearance. A project
description or disclaimer does not itself override third-party rights.

The AGPL license below covers the software rights granted by this project's
contributors and the relevant upstream licensors. It grants no permission to
third parties' proprietary game content or official services.

## License, source, and attribution

The maintenance code, startup helper, scripts, tests, and documentation in this
repository are distributed under the **GNU Affero General Public License,
version 3**; see [LICENSE](LICENSE). The full license text is retained unchanged.
The complete source and helper build script accompany the precompiled startup
helper in the maintenance archive. Documentation and publication notices were
updated on 27 October 2026; the application version is 0.1.4.

Upstream projects retain their authorship, copyright notices, and license terms:

| Project | Role | Source and license |
| --- | --- | --- |
| EpinelPS | Server implementation; compiled builds are downloaded separately at runtime. | [Upstream source](https://github.com/EpinelPS/EpinelPS) · [AGPL-3.0 license](https://github.com/EpinelPS/EpinelPS/blob/main/LICENSE) |
| EpinelPSLauncher | Separate Linux launcher; compatibility reference for game-side trust handling. Its executable is not bundled here. | [Upstream source](https://github.com/EpinelPS/EpinelPSLauncher) · [AGPL-3.0 license](https://github.com/EpinelPS/EpinelPSLauncher/blob/master/LICENSE) |

When distributing covered software, preserve applicable notices and satisfy the
AGPL's corresponding-source obligations. If you modify an AGPL-covered network
program and let users interact with that version remotely, section 13 also
requires the applicable corresponding-source offer. Consult the
[license text](https://www.gnu.org/licenses/agpl-3.0.html) for the actual terms.
This README adds no noncommercial-only or education-only restriction to the AGPL.

Third-party game software, characters, artwork, audio, trademarks, and related
intellectual property remain the property of their respective rights holders.
No ownership of or license to that proprietary material is claimed or granted.

The software is provided without warranty, subject to the terms and limits of
[LICENSE](LICENSE) and applicable law. Its availability on GitHub, the absence of
bundled game assets, or this notice does not guarantee protection from a rights
claim, account action, or takedown. GitHub describes its copyright and circumvention
claim process in its [DMCA policy](https://docs.github.com/en/site-policy/content-removal-policies/dmca-takedown-policy).

For project-specific rights concerns, contact the
[repository maintainer](https://github.com/seba970423) using any published contact
details and identify the affected files and rights at issue. Formal GitHub legal
notices should use GitHub's published procedures. Do not include credentials,
private keys, account data, or copyrighted game packs in public issues.
