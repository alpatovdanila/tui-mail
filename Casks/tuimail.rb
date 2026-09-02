cask "tuimail" do
  version "1.12.0"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"

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
