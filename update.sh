#!/bin/bash
# Обновление аналитики: выгрузка из GetCourse → расчёт → шифрование → публикация на GitHub Pages.
# Запускается автоматически раз в 2 часа (launchd), можно запустить вручную: ./update.sh
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
LOG=update.log
exec >>"$LOG" 2>&1
echo "=== $(date '+%d.%m.%Y %H:%M') ==="

PW_FILE="ПАРОЛЬ_САЙТА.txt"
if [ ! -s "$PW_FILE" ] || grep -q "ВСТАВЬТЕ" "$PW_FILE"; then
  echo "Нет пароля сайта в $PW_FILE — пропуск"; exit 1
fi

# если свежая выгрузка уже есть (моложе 90 минут) — не тратим лимит GetCourse повторно
fresh() { [ -f "raw/$1.json" ] && [ -n "$(find "raw/$1.json" -mmin -90)" ]; }
fresh deals || python3 gc_export.py deals 2026-07-01
fresh group_4897279 || python3 gc_export.py group 4897279
SITE_PASSWORD="$(tr -d '\r\n' < "$PW_FILE")" python3 build.py
rm -f site/data.plain.json

# публикация: ветка gh-pages содержит только страницу и зашифрованные данные, без истории
PUB=$(mktemp -d)
cp site/index.html site/data.enc.json "$PUB"/
touch "$PUB/.nojekyll"
REMOTE=$(git remote get-url origin)
cd "$PUB"
git init -q -b gh-pages
git add -A
git -c user.name="club-analytics" -c user.email="noreply@users.noreply.github.com" commit -q -m "Обновление $(date '+%d.%m %H:%M')"
git push -q -f "$REMOTE" gh-pages
cd - >/dev/null
rm -rf "$PUB"
echo "Опубликовано"
