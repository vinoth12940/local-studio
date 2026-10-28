# Template for a Homebrew cask, as Murmur's packaging/homebrew/murmur.rb. Copy it into a tap repository (for example
# vinoth12940/homebrew-local-studio, under Casks/local-studio.rb) and update `version` and `sha256` after each release.
cask "local-studio" do
  version "1.6.0"
  sha256 "REPLACE_WITH_THE_SHA256_FROM_Local-Studio-VERSION.dmg.sha256"

  url "https://github.com/vinoth12940/local-studio/releases/download/v#{version}/Local-Studio-#{version}.dmg"
  name "Local Studio"
  desc "Private, on-device AI studio: chat, coding agent, voice, file search and images"
  homepage "https://github.com/vinoth12940/local-studio"

  depends_on arch: :arm64
  depends_on macos: ">= :sonoma"

  app "Local Studio.app"

  # Models live in the shared Hugging Face cache and are left alone, as uninstall.sh does.
  zap trash: [
    "~/Library/Application Support/Local Studio",
    "~/.cache/mlx_dspark",
  ]
end
