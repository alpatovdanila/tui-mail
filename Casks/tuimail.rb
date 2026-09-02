cask "tuimail" do
  version "1.13.0"
  sha256 "2ccae4a289e186b1fb5a247dfe9c843cb9091fc90290414d98fcf84f2a1748a3"

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
