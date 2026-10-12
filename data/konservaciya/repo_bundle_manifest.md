# Бэкап репозитория одним bundle

Сделан прогоном run_backup_repo_bundle.yml, метка 2026-10-12.

| поле | значение |
|---|---|
| файл | `repo-2026-10-12.bundle` |
| размер | 7721880705 байт (7364 МБ) |
| sha256 | `85dc64baa11c38eec7c466e5333375b658dfa9c8e805ffdea0607dc00fb105c0` |
| ветки | 11 |
| теги | 0 |
| прогон | https://github.com/Russiansky777/robinhood-chain-alpha/actions/runs/38189721935 |

## Как восстановить репозиторий из этого файла

```bash
echo '85dc64baa11c38eec7c466e5333375b658dfa9c8e805ffdea0607dc00fb105c0  repo-2026-10-12.bundle' | sha256sum -c -        # файл не побился
git clone --mirror repo-2026-10-12.bundle repo-mirror.git   # все ветки и теги
git clone repo-mirror.git robinhood-chain-alpha   # рабочий клон
cd robinhood-chain-alpha && git branch -a         # ветки на месте
```

Одной строкой, если нужна только рабочая ветка:

```bash
git clone -b claude/nifty-sagan-r0polg repo-2026-10-12.bundle robinhood-chain-alpha
```

Залить в НОВЫЙ пустой репозиторий на новом аккаунте:

```bash
cd repo-mirror.git && git push --mirror https://github.com/NOVYJ/REPO.git
```

## Ветки в bundle

- `claude/bootstrap-nonblocking-fix` -- 93693327b, 2026-09-13
- `claude/c2-hedge-analysis` -- 32518e4d8, 2026-09-24
- `claude/chat-update-nmh1sv` -- c82581dca, 2026-09-14
- `claude/nifty-sagan-r0polg` -- d4c9be557, 2026-10-12
- `claude/podbivka` -- 2d8493657, 2026-10-12
- `claude/robinhood-copytrading-hypothesis-awjj0g` -- 2584d9389, 2026-10-08
- `claude/sc1-pons-v2-launcher-efdbyt` -- a56b09b74, 2026-09-14
- `claude/sc1-verdict-3jaxoy` -- b119cd5d0, 2026-09-01
- `claude/stroiteli` -- 7113b8943, 2026-10-11
- `dashboard` -- c0a68d33e, 2026-09-20
- `main` -- ccba88ca5, 2026-09-20

## Чем так много

Вес даёт не код, а исследовательские архивы в истории:
data/podbivka и data/sprintG1_cache -- крупнейшие блобы по
55-96 МБ каждый. Сам код -- единицы МБ.
