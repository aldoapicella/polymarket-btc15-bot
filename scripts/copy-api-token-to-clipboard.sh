#!/usr/bin/env bash
set -euo pipefail

token_file="${1:-data/api-bearer-token.txt}"

if [[ ! -r "$token_file" ]]; then
  printf 'Token file not found or not readable: %s\n' "$token_file" >&2
  exit 1
fi

if command -v clip.exe >/dev/null 2>&1; then
  tr -d '\r\n' < "$token_file" | clip.exe
elif command -v wl-copy >/dev/null 2>&1; then
  tr -d '\r\n' < "$token_file" | wl-copy
elif command -v xclip >/dev/null 2>&1; then
  tr -d '\r\n' < "$token_file" | xclip -selection clipboard
elif command -v xsel >/dev/null 2>&1; then
  tr -d '\r\n' < "$token_file" | xsel --clipboard --input
elif command -v pbcopy >/dev/null 2>&1; then
  tr -d '\r\n' < "$token_file" | pbcopy
else
  printf 'No supported clipboard command found. Token was not printed.\n' >&2
  printf 'Install wl-copy, xclip, xsel, or run from WSL with clip.exe available.\n' >&2
  exit 1
fi

printf 'API bearer token copied to clipboard. It was not printed.\n'
