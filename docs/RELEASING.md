# Releasing Local Studio

A release is a signed, notarized `Local-Studio-<version>.dmg` (and its `.sha256`) attached to a GitHub release, the same
way Murmur ships. Signed and notarized means people open it with a double-click: no "unidentified developer" warning, and
macOS keeps the same permissions (microphone, folders) across updates. The README's **Download for Mac** link goes to the
latest release.

## One-time setup

### 1. A Developer ID Application certificate

Distribution outside the Mac App Store needs a **Developer ID Application** certificate; *Apple Development* and *Apple
Distribution* certificates cannot be notarized for it. Check that it is in your keychain (`build.sh` signs with it
automatically when it is):

```bash
security find-identity -v -p codesigning | grep "Developer ID Application"
```

### 2. Notarization credentials: an App Store Connect API key

Use an **App Store Connect API key** (Users and Access → Integrations → App Store Connect API: note the Key ID and
the Issuer ID, and download `AuthKey_<KeyID>.p8` once). `notarytool` takes the key directly, so no keychain item is
involved. Murmur's first release found the keychain profile unreliable:
- the app's notarization was accepted with `murmur-notary`;
- seconds later, the DMG's submission failed with "No Keychain password item found for profile";
- re-storing the profile did not stick, and only the key passed directly worked.

The same profile vanished here on 2026-10-28.

Keep the key in a file outside the repository that sets three variables, as Murmur's `~/private_keys/asc.env` does:

```bash
ASC_KEY_PATH=/path/to/AuthKey_<KeyID>.p8
ASC_KEY_ID=<KeyID>
ASC_ISSUER_ID=<IssuerID>
```

Make it and the `.p8` readable only by you (`chmod 600`). Then point `release.sh` at it with
`LOCAL_STUDIO_NOTARY_ENV=~/private_keys/asc.env`. The script sources it and passes the key straight to `notarytool`,
and the build and its checks never see the values. `LOCAL_STUDIO_NOTARY_KEY`, `_KEY_ID` and `_ISSUER` (or `ASC_*`)
set directly work too.

A keychain profile (`LOCAL_STUDIO_NOTARY_PROFILE`, default `notary`) still works as a fallback. Create it with
`xcrun notarytool store-credentials notary --apple-id "you@example.com" --team-id "TEAMID"` and an app-specific password.
`release.sh --publish` checks the credentials before building and refuses to start without working ones.

### How long notarization takes

- Apple held Murmur's first app submission "In Progress" for **29 minutes**; its DMG, right after, took about 30
  seconds. A team's or an app's first submissions are the slow ones.
- `release.sh` waits up to `LOCAL_STUDIO_NOTARY_TIMEOUT` (default `2h`) for each of its two submissions (app, then DMG)
  and prints each submission id.
- A long wait is not a failure. Ask Apple directly with
  `xcrun notarytool history --key <p8> --key-id <id> --issuer <issuer>`, or with `xcrun notarytool info <id> ...`.
- Run the release where a half-hour wait is fine (in the background, or in a terminal you leave open).

## Cutting a release from your Mac

1. Bump `APP_VERSION` in `app.py` (and the README's version), and give `CHANGELOG.md` a dated entry:
   `## 1.7.0 (2026-11-02)`.
2. Merge to `main` with CI green, then on `main`:

   ```bash
   git switch main && git pull --ff-only
   LOCAL_STUDIO_NOTARY_ENV=~/private_keys/asc.env macos/release.sh --publish
   ```

`release.sh` runs the dependency audits and `build.sh` (every check), then:
- notarizes and staples the app;
- puts it in the DMG, then signs, notarizes and staples the DMG;
- checks the DMG the way a user's Mac will (`stapler validate`, `spctl --assess --type open`), then mounts it and checks
  the app inside (`spctl --type execute`, `stapler validate`), as Murmur's release did;
- writes `build/Local-Studio-<version>.dmg.sha256`;
- publishes release `v<version>` with both files. The notes are the changelog entry plus the SHA-256.

It refuses to publish:
- a build that is not notarized;
- from anything but a clean `main` equal to `origin/main`;
- a version that is already released;
- a changelog entry that is missing or still says `unreleased`.

Without `--publish` it only builds the DMG. It then says whether the DMG is *signed and notarized*, *signed but NOT
notarized* or *not signed with a Developer ID*. Only the first is fit to share.

Check a DMG by hand:

```bash
spctl --assess --type open --context context:primary-signature -v build/Local-Studio-1.6.0.dmg
xcrun stapler validate build/Local-Studio-1.6.0.dmg
```

## Releasing from GitHub Actions instead

Pushing a `v*` tag runs `.github/workflows/release.yml`, which builds on GitHub's Apple Silicon runner, then signs,
notarizes and publishes there. It needs these repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `LOCAL_STUDIO_CERT_P12` | The Developer ID Application certificate exported from Keychain Access as a `.p12`, then `base64 -i cert.p12 \| pbcopy` |
| `LOCAL_STUDIO_CERT_PASSWORD` | The password you set when exporting the `.p12` |
| `LOCAL_STUDIO_ASC_KEY_P8` | The contents of `AuthKey_<KeyID>.p8` (preferred, as above) |
| `LOCAL_STUDIO_ASC_KEY_ID`, `LOCAL_STUDIO_ASC_ISSUER_ID` | Its Key ID and Issuer ID |
| `LOCAL_STUDIO_APPLE_ID`, `LOCAL_STUDIO_TEAM_ID`, `LOCAL_STUDIO_APP_PASSWORD` | Instead of the key: Apple ID email, Team ID, app-specific password |

The job may run for up to four hours, for a slow first notarization. Without those secrets, or when the release already exists, the workflow skips with a notice instead of failing.
`release.sh --publish` creates the tag, so its own run always takes the second path. Anyone who can change workflows on
`main` can use the certificate, so add the secrets only if you want releases built without your Mac.

> **Not yet verified:** the workflow has never run with secrets. Do the first release locally.

## Homebrew

`packaging/homebrew/local-studio.rb` is a cask template. To offer `brew install --cask local-studio`:

1. Create a public repository named `homebrew-local-studio`.
2. Copy the template to `Casks/local-studio.rb` in it.
3. After each release, set `version`, and set `sha256` to the first field of `Local-Studio-<version>.dmg.sha256`.
