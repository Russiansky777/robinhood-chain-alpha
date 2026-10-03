#!/bin/sh
# Потолок диска лаборатории: держит кэш архива в рамках и чистит журналы
# бегунка. Ставится прогоном run_lab_uborka_nl.yml как oneshot-служба с
# таймером раз в час.
#
# ЗАЧЕМ ОН ВООБЩЕ. 03.10 на lab-miami кончился диск: прогоны падали с
# "No space left on device", zakem2 отработал 8 суток из 11, суточный не
# стартовал. Одна разовая чистка привела бы к тому же через неделю, поэтому
# потолок держит сама машина -- без прогона и без сессии.
#
# ТРОГАЕТ ТОЛЬКО НАЗВАННЫЕ ПУТИ:
#   * _diag бегунка -- его собственные журналы запусков, старше двух суток;
#   * $HOME/arhiv_pumpapi -- кэш архива pumpapi (восстановим из источника);
#   * журналы systemd -- до 200 МБ.
# Ничего другого не удаляется ни при каких значениях переменных.
set -e

# ПОТОЛОК ПРИХОДИТ ОКРУЖЕНИЕМ, а умолчание стоит здесь: если служба окажется
# без Environment=, скрипт не должен молча счистить всё до нуля.
POTOLOK_B="${POTOLOK_B:-5368709120}"

case "$POTOLOK_B" in
    ''|*[!0-9]*)
        echo "OTKAZ: POTOLOK_B='$POTOLOK_B' ne chislo -- nichego ne chistim" >&2
        exit 1
        ;;
esac
# НОЛЬ ИЛИ КРОШКА -- ТОЖЕ ОТКАЗ. Потолок меньше гигабайта почти наверняка
# опечатка (байты вместо гигабайт), а он снёс бы кэш целиком.
if [ "$POTOLOK_B" -lt 1073741824 ]; then
    echo "OTKAZ: potolok $POTOLOK_B bajt menshe gigabajta -- pohozhe na opechatku" >&2
    exit 1
fi

# 1. ЖУРНАЛЫ БЕГУНКА. Он их только дописывает и сам не чистит.
for D in /home/*/actions-runner; do
    [ -d "$D/_diag" ] || continue
    N=$(find "$D/_diag" -type f -mtime +2 | wc -l)
    find "$D/_diag" -type f -mtime +2 -delete 2>/dev/null || true
    [ "$N" -gt 0 ] && echo "_diag $D: ubrano $N fajlov starshe dvuh sutok"
done

# 2. КЭШ АРХИВА ДО ПОТОЛКА -- с САМЫХ СТАРЫХ файлов. Не "rm -rf каталог":
#    свежая часть кэша полезна, и выбрасывать её значит заставить зонд качать
#    всё заново.
for H in /root /home/*; do
    A="$H/arhiv_pumpapi"
    [ -d "$A" ] || continue
    BYLO=$(du -sb "$A" | cut -f1)
    if [ "$BYLO" -le "$POTOLOK_B" ]; then
        continue
    fi
    find "$A" -type f -printf '%T@ %s %p\n' 2>/dev/null | sort -n | awk -v cel="$POTOLOK_B" -v cur="$BYLO" '
        { if (cur <= cel) exit; print $3; cur -= $2 }
    ' | while IFS= read -r F; do
        rm -f -- "$F"
    done
    find "$A" -type d -empty -delete 2>/dev/null || true
    STALO=$(du -sb "$A" 2>/dev/null | cut -f1 || echo 0)
    echo "arhiv $A: bylo $((BYLO / 1073741824)) GB, stalo $((STALO / 1073741824)) GB pri potolke $((POTOLOK_B / 1073741824)) GB"
done

# 3. ЖУРНАЛЫ SYSTEMD: растут сами, к работе не нужны.
journalctl --vacuum-size=200M >/dev/null 2>&1 || true

echo "potolok diska: gotovo, svobodno $(df -h --output=avail / | tail -1 | tr -d ' ')"
