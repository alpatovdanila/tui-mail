cask "tuimail" do
  version "1.13.1"
  sha256 "e2889c620754d01db418650bf48f947b7e7338f2f9349e3bc92bbc7be7de937c"

  url "https://github.com/alpatovdanila/tui-mail/releases/download/v#{version}/tuimail-macos-universal.tar.gz"
  name "tuimail"
  desc "Keyboard-first email client for the terminal"
  homepage "https://github.com/alpatovdanila/tui-mail"

  livecheck do
    url :url
    strategy :github_latest
  end

  binary "tuimail"

  zap trash: "~/.tuimail.json"
end
