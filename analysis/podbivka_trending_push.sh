#!/usr/bin/env bash
# Суточный пуш снимков трендинга в ветку. Кладёт только data/trending -- торгового пути
# и ключей не касается. Запускается таймером podbivka-trending-push.timer на lab-miami.
# Ветка берётся из PODB_VETKA, по умолчанию claude/podbivka.
set -u
КОРЕНЬ="${PODB_KOREN:-/opt/robinhood-chain-alpha}"
ВЕТКА="${PODB_VETKA:-claude/podbivka}"
cd "$КОРЕНЬ" || exit 1

# Пушим только из своей ветки: чужую историю в неё не затащим.
ТЕК=$(git rev-parse --abbrev-ref HEAD)
[ "$ТЕК" = "$ВЕТКА" ] || { echo "checkout на $ТЕК, а не $ВЕТКА -- не пушу"; exit 1; }

git add -- data/trending || exit 1
git diff --cached --quiet -- data/trending && { echo "нечего пушить"; exit 0; }

ЧИСЛО=$(git diff --cached --numstat -- data/trending | wc -l)
git -c user.name="podbivka-trending" -c user.email="noreply@anthropic.com" \
    commit -q -m "трендинг: суточные снимки ($ЧИСЛО файлов)" -- data/trending || exit 1

for П in 2 4 8 16 0; do
    git push -u origin "HEAD:refs/heads/$ВЕТКА" && { echo "пуш готов"; exit 0; }
    [ "$П" = 0 ] && break
    echo "пуш не прошёл, жду ${П}s"; sleep "$П"
done
echo "пуш не прошёл после 4 попыток"; exit 1
