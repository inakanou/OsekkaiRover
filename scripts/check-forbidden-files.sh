#!/usr/bin/env bash
# Fail if Git tracks files that must never be in this public repository:
# personal settings, records, face features, media, archives.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Extended regexes matched against each tracked path.
forbidden=(
  '^config/local\.yaml$'          # child's name, times, remote pin
  '(^|/)data/'                    # records, calibration, caches
  '\.npz$'                        # face features (biometric data)
  '\.(wav|mp3|h264|mp4)$'         # audio and video
  '\.(zip|tar|tgz|gz|7z)$'        # archives
  '\.env$'                        # secret env files (.env, *.env)
)
# Photos are allowed only under docs/ (screenshots for documentation).
images='\.(jpe?g|png)$'

found=0
while IFS= read -r -d '' f; do
  for re in "${forbidden[@]}"; do
    if [[ "$f" =~ $re ]]; then
      echo "禁止ファイル: $f" >&2
      found=1
    fi
  done
  if [[ "$f" =~ $images && ! "$f" =~ ^docs/ ]]; then
    echo "禁止ファイル（画像は docs/ の下だけ）: $f" >&2
    found=1
  fi
done < <(git ls-files -z)

if (( found )); then
  echo "公開リポジトリに入れてはいけないファイルがあります。git rm --cached で外してください。" >&2
  exit 1
fi
echo "禁止ファイルはありません。"
