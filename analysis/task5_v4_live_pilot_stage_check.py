#!/usr/bin/env python3
"""Задача 5: ЧИСТО ЧИТАЮЩАЯ (по указанию владельца: "без нового аудита и
повторного скана") проверка ТОЧНОЙ стадии bootstrap_registry() уже
запущенного LIVE-пилота (run34771657422_1) -- читает ТОЛЬКО уже
существующий лог процесса (весь файл, не только хвост) и ищет реальные
[route_registry]-тэгированные строки (bootstrap_registry() печатает их
на каждом реальном шаге -- восстановление/сид, discovery, живучесть,
итог) -- честно называет ПОСЛЕДНЮЮ достигнутую стадию, БЕЗ придумывания
процента/времени там, где счётчика прогресса в коде нет (refresh_liveness_all
не печатает по маршруту -- см. её исходник, только один финальный
подсчёт после ВСЕГО прохода). Отдельно перепроверяет PID/ps -- НЕ
полагается на статус GitHub-джобы."""
import json
import re
import subprocess
from pathlib import Path

LOG_FILE = Path("/home/bot/data/task5_v4_hotpath_live_run34771657422_1.log")
PID_FILE = Path("/home/bot/data/task5_v4_hotpath_live_run34771657422_1.pid")

STAGE_TAGS = ("[route_registry]", "[live]", "[registry-worker]")


def main() -> None:
    result: dict = {}

    if not LOG_FILE.exists():
        result["ok"] = False
        result["error"] = f"лог не найден: {LOG_FILE}"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    all_lines = LOG_FILE.read_text(errors="replace").splitlines()
    result["total_log_lines"] = len(all_lines)

    stage_lines = [l for l in all_lines if any(t in l for t in STAGE_TAGS)]
    result["n_stage_tagged_lines"] = len(stage_lines)
    result["all_route_registry_or_live_or_worker_lines"] = stage_lines

    # Честный счёт активности quote-подсистемы (НЕ равно "маршрутов
    # обработано" -- один маршрут может дать НЕСКОЛЬКО quote-вызовов,
    # по плечу; это просто объём RPC-активности, помечено честно).
    quote_replay_lines = [l for l in all_lines if "[v4_quote_replay]" in l]
    result["n_v4_quote_replay_log_lines_total"] = len(quote_replay_lines)
    result["n_quote_replay_alchemy_fallback_succeeded"] = sum(
        1 for l in quote_replay_lines if "дал результат там" in l)
    result["n_quote_replay_alchemy_fallback_also_failed"] = sum(
        1 for l in quote_replay_lines if "тоже отказал" in l)

    # Последняя РЕАЛЬНАЯ стадийная строка -- определяет, на каком шаге
    # bootstrap_registry() (или уже пост-bootstrap воркер) сейчас.
    result["last_stage_line"] = stage_lines[-1] if stage_lines else None

    # Пытаемся честно вытащить "проверяю живучесть всех N маршрутов" --
    # ДАЁТ total, но НЕ даёт "сколько уже сделано" (в коде такого
    # счётчика нет -- refresh_liveness_all не логирует по одному
    # маршруту, см. её докстринг/тело).
    m = re.search(r"проверяю живучесть всех (\d+) маршрутов на блоке (\d+)", "\n".join(stage_lines))
    if m:
        result["liveness_check_total_routes_announced"] = int(m.group(1))
        result["liveness_check_at_block"] = int(m.group(2))
    result["liveness_check_has_completed_line"] = any(
        "порционную проверку живучести завершена" in l or "ИТОГ: живых" in l for l in stage_lines
    )
    result["no_progress_counter_in_refresh_liveness_all"] = True
    result["note_on_progress"] = (
        "refresh_liveness_all() (полный, НЕ порционный проход, вызывается ОДИН раз внутри bootstrap_registry()) "
        "печатает ТОЛЬКО 'проверяю живучесть всех N маршрутов' ДО начала и ничего по ходу -- следующая "
        "[route_registry]-строка появится только когда пройдут ВСЕ N (или процесс перейдёт дальше). Честно: "
        "'сколько обработано из скольких' здесь недоступно, оценивать время не буду."
    )

    m2 = re.search(r"найдено новых пулов/циклов арбитражника: (\d+) маршрутов", "\n".join(stage_lines))
    if m2:
        result["discovery_new_routes_found"] = int(m2.group(1))
    m3 = re.search(r"ВОССТАНОВЛЕНО состояние из .*?: (\d+) маршрутов, курсор=(\d+)", "\n".join(stage_lines))
    if m3:
        result["resumed_from_saved_state"] = True
        result["resumed_routes_count"] = int(m3.group(1))
        result["resumed_cursor_block"] = int(m3.group(2))
    else:
        result["resumed_from_saved_state"] = False

    # PID -- независимая перепроверка, НЕ полагаемся на статус GitHub job.
    pid = PID_FILE.read_text().strip() if PID_FILE.exists() else None
    result["pid_from_file"] = pid
    if pid:
        ps_proc = subprocess.run(["ps", "-p", pid, "-o", "pid,ppid,stat,etime,cmd", "--no-headers"],
                                  capture_output=True, text=True, timeout=10)
        result["ps_output"] = ps_proc.stdout.strip() or ps_proc.stderr.strip()
        result["process_alive_now"] = ps_proc.returncode == 0 and bool(ps_proc.stdout.strip())
        proc_status = Path(f"/proc/{pid}/status")
        if proc_status.exists():
            result["proc_state_line"] = next(
                (l for l in proc_status.read_text().splitlines() if l.startswith("State:")), None)

    result["ok"] = True
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
