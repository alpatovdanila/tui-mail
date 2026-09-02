#!/bin/sh
# tuimail installer - curl -fsSL https://raw.githubusercontent.com/alpatovdanila/tui-mail/main/install.sh | sh
# Installs the latest release to ~/.local/bin (no sudo), sha256-verified.
# Knobs: TUIMAIL_BIN_DIR overrides the target dir; TUIMAIL_NO_PATH=1 skips the PATH edit.
set -eu

REPO="alpatovdanila/tui-mail"
BIN_DIR="${TUIMAIL_BIN_DIR:-$HOME/.local/bin}"

case "$(uname -s)" in
  Darwin) ASSET="tuimail-macos-universal" ;;
  *)
    echo "No prebuilt binary for $(uname -s). Install with Python instead:"
    echo "  pipx install git+https://github.com/${REPO}.git"
    exit 1
    ;;
esac

echo "Looking up the latest tuimail release..."
API_JSON="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest")"
TAG="$(printf '%s' "$API_JSON" | grep -m1 '"tag_name"' | cut -d'"' -f4)"
[ -n "${TAG}" ] || { echo "Could not determine the latest release."; exit 1; }
URL="https://github.com/${REPO}/releases/download/${TAG}/${ASSET}"
SHA="$(printf '%s' "$API_JSON" | awk -v a="\"name\": \"${ASSET}\"" '
  index($0, a) {found=1}
  found && /"digest":/ {gsub(/.*sha256:/, ""); gsub(/".*/, ""); print; exit}')"

TMP="$(mktemp)"
trap 'rm -f "${TMP}"' EXIT
echo "Downloading tuimail ${TAG}..."
curl -fL --progress-bar "${URL}" -o "${TMP}"

if [ -n "${SHA}" ]; then
  GOT="$(shasum -a 256 "${TMP}" | cut -d' ' -f1)"
  if [ "${GOT}" != "${SHA}" ]; then
    echo "Integrity check FAILED - not installing." >&2
    exit 1
  fi
  echo "sha256 verified."
else
  echo "warning: release digest unavailable - relying on TLS only." >&2
fi

mkdir -p "${BIN_DIR}"
mv "${TMP}" "${BIN_DIR}/tuimail"
trap - EXIT
chmod +x "${BIN_DIR}/tuimail"

if [ -z "${TUIMAIL_NO_PATH:-}" ]; then
  case ":$PATH:" in
    *":${BIN_DIR}:"*) ;;
    *)
      case "${SHELL:-}" in
        */bash) RC="$HOME/.bashrc" ;;
        *) RC="$HOME/.zshrc" ;;
      esac
      printf '\nexport PATH="%s:$PATH"\n' "${BIN_DIR}" >> "${RC}"
      echo "Added ${BIN_DIR} to PATH in ${RC} - open a new terminal (or: export PATH=\"${BIN_DIR}:\$PATH\")."
      ;;
  esac
fi

echo "Installed tuimail ${TAG} to ${BIN_DIR}/tuimail - run: tuimail"
