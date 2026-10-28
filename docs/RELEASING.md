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

### 2. Notarization credentials

`release.sh` uses the `notarytool` keychain profile named in `LOCAL_STUDIO_NOTARY_PROFILE` (default `notary`). A profile
belongs to your Apple ID and team, not to an app, so the one Murmur uses works here too:

```bash
export LOCAL_STUDIO_NOTARY_PROFILE=murmur-notary
```

To create one, make an **app-specific password** at appleid.apple.com → Sign-In and Security → App-Specific Passwords, then
store it once (the command asks for the password; nothing is written to the repository):

```bash
xcrun notarytool store-credentials notary --apple-id "you@example.com" --team-id "TEAMID"
```

Or use an App Store Connect API key instead of a password: set `LOCAL_STUDIO_NOTARY_KEY` (the `.p8` path),
`LOCAL_STUDIO_NOTARY_KEY_ID` and `LOCAL_STUDIO_NOTARY_ISSUER`. Keep the key file out of the repository.

Check the credentials right before a release with `xcrun notarytool history --keychain-profile <name>`. A saved
profile can vanish from the login keychain: Murmur's guide records it, and on 2026-10-28 `murmur-notary` answered once
and then reported "No Keychain password item found". The API key variables avoid the keychain entirely.
`release.sh --publish` checks the credentials before building and refuses to start without them.

## Cutting a release from your Mac

1. Bump `APP_VERSION` in `app.py` (and the README's version), and give `CHANGELOG.md` a dated entry:
   `## 1.7.0 (2026-11-02)`.
2. Merge to `main` with CI green, then on `main`:

   ```bash
   git switch main && git pull --ff-only
   LOCAL_STUDIO_NOTARY_PROFILE=murmur-notary macos/release.sh --publish
   ```

`release.sh` runs the dependency audits and `build.sh` (every check), then:
- notarizes and staples the app;
- puts it in the DMG, then signs, notarizes and staples the DMG;
- checks the DMG the way a user's Mac will (`stapler validate`, `spctl --assess --type open`);
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
| `LOCAL_STUDIO_APPLE_ID` | Your Apple ID email |
| `LOCAL_STUDIO_TEAM_ID` | Your 10-character Team ID |
| `LOCAL_STUDIO_APP_PASSWORD` | An app-specific password |

Without those secrets, or when the release already exists, the workflow skips with a notice instead of failing.
`release.sh --publish` creates the tag, so its own run always takes the second path. Anyone who can change workflows on
`main` can use the certificate, so add the secrets only if you want releases built without your Mac.

> **Not yet verified:** the workflow has never run with secrets. Do the first release locally.

## Homebrew

`packaging/homebrew/local-studio.rb` is a cask template. To offer `brew install --cask local-studio`:

1. Create a public repository named `homebrew-local-studio`.
2. Copy the template to `Casks/local-studio.rb` in it.
3. After each release, set `version`, and set `sha256` to the first field of `Local-Studio-<version>.dmg.sha256`.
