cask "tuimail" do
  version "1.12.1"
  sha256 "d0d9b92c1f83c76a886ad9ba310053e691aad6a0e3e451e81a7a96c316460f99"

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
