#!/usr/bin/env python3
"""Проверка workflow: YAML, shell и -- главное -- heredoc.

bash -n незакрытый heredoc ПРИНИМАЕТ: он считает, что тело идёт до конца
файла. Поэтому отступленный терминатор (а внутри run: | он обязан быть
отступлен, иначе рвётся блочный скаляр YAML) проходит обе прежние
проверки и ломается только на прогоне. Отсюда правило: внутри run: |
heredoc не используем вовсе.
"""
import glob
import os
import re
import subprocess
import sys
import tempfile

import yaml

HEREDOC = re.compile(r"<<-?\s*'?\"?([A-Za-z_][A-Za-z0-9_]*)'?\"?")
# Присваивание переменной с не-латинским именем. bash требует
# [a-zA-Z_][a-zA-Z0-9_]*, поэтому "ИМЯ=значение" он разбирает как запуск
# команды с таким именем: шаг падает, а bash -n молчит.
# Имя обязано быть словом целиком (без | и кавычек) и стоять в начале
# команды. Иначе ловится содержимое строк на других языках: первый вариант
# поймал "медиана|ошибки|=%s" внутри питоновского print.
НЕЛАТИНСКАЯ_ПЕРЕМЕННАЯ = re.compile(
    r"(?:^\s*|[;&]\s*|\|\|\s*|&&\s*)([^\W\d_]\w*)=(?![=~])", re.UNICODE)


def main() -> int:
    плохо = []
    файлы = sorted(glob.glob(".github/workflows/*.yml")) + sorted(glob.glob(".github/workflows/*.yaml"))
    for f in файлы:
        try:
            d = yaml.safe_load(open(f, encoding="utf-8"))
        except Exception as exc:
            плохо.append((f, "-", f"YAML: {str(exc).splitlines()[0]}"))
            continue
        if not isinstance(d, dict) or not isinstance(d.get("jobs"), dict):
            continue
        for j in d["jobs"].values():
            if not isinstance(j, dict):
                continue
            for st in j.get("steps") or []:
                r = (st or {}).get("run")
                if not isinstance(r, str):
                    continue
                имя = (st or {}).get("name", "?")
                tf = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8")
                tf.write(r)
                tf.close()
                rc = subprocess.run(["bash", "-n", tf.name], capture_output=True, text=True)
                os.unlink(tf.name)
                if rc.returncode:
                    плохо.append((f, имя, f"shell: {rc.stderr.strip().splitlines()[0][:120]}"))
                # YAML снимает отступ блочного скаляра, поэтому в РАЗОБРАННОЙ
                # строке терминатор правильного heredoc стоит в нулевой
                # колонке -- это норма. Ошибка ровно одна: терминатора с
                # нулевым отступом нет вовсе. Тогда heredoc не закрыт, и
                # bash -n это пропускает, считая тело идущим до конца файла.
                # Случай "тело в нулевой колонке в ИСХОДНОМ файле" ловится
                # разбором YAML выше: такой файл просто не разбирается.
                строки = r.splitlines()
                # Тело heredoc пропускаем: там чужой язык, и его строки
                # под правила shell не попадают.
                в_heredoc = None
                for стр in строки:
                    if в_heredoc is not None:
                        if стр.rstrip() == в_heredoc and not стр[:1].isspace():
                            в_heredoc = None
                        continue
                    m_h = HEREDOC.search(стр)
                    if m_h:
                        в_heredoc = m_h.group(1)
                        continue
                    без_комментария = стр.split("#", 1)[0]
                    for m in НЕЛАТИНСКАЯ_ПЕРЕМЕННАЯ.finditer(без_комментария):
                        имя_пер = m.group(1)
                        if имя_пер.isascii():
                            continue
                        плохо.append((f, имя, f"переменная shell '{имя_пер}' не "
                                       "латиницей: bash так не умеет, шаг упадёт, "
                                       "а bash -n этого не видит"))
                for стр in строки:
                    m = HEREDOC.search(стр)
                    if not m:
                        continue
                    метка = m.group(1)
                    закрыт = any(x.rstrip() == метка and not x[:1].isspace()
                                 for x in строки)
                    if not закрыт:
                        плохо.append((f, имя, f"heredoc <<{метка} НЕ закрыт: "
                                       "терминатор отступлен или отсутствует, "
                                       "bash -n этого не видит"))
    for f, имя, почему in плохо:
        print(f"{f} | {имя} | {почему}")
    print(f"\nпроверено файлов: {len(файлы)}, замечаний: {len(плохо)}")
    return 1 if плохо else 0


if __name__ == "__main__":
    sys.exit(main())
