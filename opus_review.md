# opus_review — аудит репозитория `vladmesh/ummanu`

> Отчёт только для чтения. Код, тесты, документация и конфигурация **не изменялись**; этот файл — единственное изменение ветки.

## 1. Ревизия, резюме и главные находки

### 1.1 Что проверялось

| Параметр | Значение |
|---|---|
| Базовый коммит | `e310833e620a41e84baae9bc66b4fab804b6c840` (`main`, merge PR #665, 2026-10-05 22:03 UTC) |
| Дата аудита | 2026-10-05 |
| Объём (tracked) | 828 файлов, 407 099 строк, 18,69 млн символов |
| `src/` | 398 файлов, 171 540 строк, 7,57 млн символов (≈1,89 млн токенов при ~4 симв./токен) |
| `tests/` | 300 файлов, 213 803 строки, 9,77 млн символов (**тестов больше, чем кода**) |
| `docs/` + `skills/` + `packaging/` | 13,8 тыс. + 2,0 тыс. + 0,8 тыс. строк; ≈1,13 млн символов |
| `scripts/` | 10 файлов, 4 116 строк, 180 тыс. символов |

**Методика.** Девять параллельных субагентов, только чтение, каждый со своей зоной: `dispatch` часть A и часть B, ядро задач/спринтов/CLI, установка/восстановление/секреты/память/infra/transition, `board`, `web`/`webproto`/`webfront`, `runtime`/`po`/`automations`, тесты, документация/skills/packaging/CI. Выводы субагентов я свёл вместе и выборочно перепроверил; такие места помечены **✔**.

Инструменты:
- `ast`/`tokenize` (размеры, доля комментариев и docstring, длина функций, структурные клоны);
- `vulture` (кандидаты в мёртвый код, каждый перепроверен `grep`, включая строковые и динамические ссылки);
- `ruff 0.16.4` — версия, закреплённая в `pyproject.toml`; запуск `--no-cache`, только чтение;
- `jsonschema` (примеры против схем);
- локальный прогон `python -m tests.broad` и `scripts/ci_test_shards.py --fast` во временном venv на Python 3.12 вне репозитория.

**Метки достоверности** (используются во всём отчёте):

| Метка | Значение |
|---|---|
| **Ф** | Факт: прочитано в коде, проверено `grep`, воспроизведено или измерено |
| **О** | Оценка: число получено расчётом с указанной погрешностью |
| **Г** | Гипотеза: правдоподобно, но не доказано, нужна проверка |
| **✔** | Оркестратор лично перепроверил место в коде или воспроизвёл |

### 1.2 Резюме

1. **Главный рычаг сокращения — проза, а не дублирование кода.**
   - Комментарии и docstring составляют **26,7 % символов `src/`**: 535 тыс. символов комментариев и 1,43 млн символов docstring, итого ≈1,96 млн символов, или ≈490 тыс. токенов (**Ф**).
   - Самые «прозаичные» пакеты:

     | Пакет | Доля прозы |
     |---|---|
     | `webproto` | 47 % |
     | `runtime` | 44 % |
     | `runtime/head` | 35 % |
     | `projects` | 53 % |

   - В отдельных файлах доля выше: `runtime/local_pty_head.py` — 52 %, `dispatch/head_vitality_policy.py` — 56 %, `runtime/head/runtime.py` — 67 % (**Ф**).
   - Большая часть этой прозы — история инцидентов, номера карточек `secretary-NNNN`/`issue:…`, «раньше было так». Сама история уже есть в git и в `docs/`.
   - Сжатие до инварианта плюс одна строка «почему» уберёт **≈450–650 тыс. символов (≈110–160 тыс. токенов) из `src/`** без изменения поведения (**О**, ±30 %).
2. **Точного дублирования кода мало.**
   - Структурно одинаковых функций верхнего уровня всего на ≈244 строки (40 групп, **Ф**).
   - Окна по 8 нормализованных строк, повторяющиеся между разными файлами, — сотни строк, в основном внутри одного файла (**Ф**).
   - Реальные, доказанные и безопасные устранения дублирования дают ≈3–4 тыс. строк по всему `src/` (≈130–180 тыс. символов, **О**). Конкретика — в §6.
3. **Гигантские файлы бьют по контексту агента сильнее, чем по общему объёму.**

   | Файл | Строк | Символов | ≈Токенов |
   |---|---|---|---|
   | `tasks.py` | 5 934 | 286 тыс. | 71 тыс. |
   | `web/pages.py` | 4 996 | 250 тыс. | — |
   | `dispatch/host.py` | 5 004 | 240 тыс. | — |
   | `runtime/local_pty_head.py` | 3 445 | 180 тыс. | — |
   | `sprints.py` | 3 824 | 174 тыс. | — |
   | `dispatch/observer.py` | 3 603 | 160 тыс. | — |
   | `webproto/sprint_reads.py` | 2 773 | 135 тыс. | — |

   - Ещё есть класс `CommandHostRuntime` на 4 021 строку (≈175 методов) и `TaskWriter` на 4 668 строк.
   - Разделение почти не уменьшает общий объём, но сокращает контекст типовой задачи в **2–7 раз** (**О**).
4. **Тестов больше, чем кода.**
   - `tests/test_dispatcher.py` — 15 191 строка, ≈181 тыс. токенов, 569 тестов. Docker-зависимость в нём включается на уровне модуля и распространяется на 12 классов, которым Postgres не нужен (**Ф**).
   - Фикстура `recovery-card-shape-1440.json` весит 247 КБ, тест читает из неё 40 строк из 1 440 (**Ф**).
5. **Документация** (≈973 тыс. символов) в основном точна: 552 вызова CLI из docs и skills разрешаются по реальному дереву argparse (**Ф**).
   - На ≈90–100 тыс. символов это закрытая история: `RENAME.md`, чек-лист A20 в `HEAD_RUNTIME.md`, устаревший `OWNED_CLEANUP.md`.
   - `PROTOCOLS.md` (365 тыс. символов) — монолит, который агенту приходится загружать целиком.
6. **Найдены реальные дефекты** (§9). Самые серьёзные:

   | Дефект | Суть | Статус |
   |---|---|---|
   | BUG-01 | Request smuggling через keep-alive после ответа 413 в `web/server.py` | **Ф**, воспроизведено, **✔** |
   | BUG-02 | Невыполнимые инструкции свежей установки (нет `.venv`, PEP 668) | **Ф** |
   | BUG-03 | Профиль `--fast` падает на текущем HEAD | **Ф ✔**, воспроизведено |
   | BUG-04…08 | Несколько крэшей CLI и падение дашборда целиком при ошибке одного источника | **Ф** |
   | BUG-09 | Расхождение двух парсеров frontmatter памяти | **Ф ✔** |
   | BUG-10 | Пустой обход orphan-workspace у steward | **Ф ✔** |
   | BUG-11 | Повторный подъём головы на старом `head.pid` | **Г**, сильные основания в коде |

7. **Гейт линтера не работает.**
   - `ruff 0.16.4` с конфигом репозитория даёт **461 замечание**: `src` 196, `scripts` 7, `tests` 258 (**Ф ✔**).
   - `ruff format --check` переформатировал бы 344 файла (**Ф ✔**).
   - В CI ruff не запускается (**Ф**), хотя `pyproject.toml` объясняет закрепление версии тем, что «линтер — часть гейта».

### 1.3 Топ-15 приоритетных находок

| # | ID | Приоритет | Суть | Где подробно |
|---|---|---|---|---|
| 1 | BUG-01 | P0 | HTTP/1.1 keep-alive: тело, отвергнутое с 413, не дочитывается, и соединение не закрывается. Остаток тела разбирается как новый запрос | §9 |
| 2 | BUG-02 | P0 | Свежая установка по README/OPERATIONS не создаёт `PRODUCT_ROOT/.venv`, который требуют все systemd-юниты | §9, §8 |
| 3 | ARCH-01 | P1 | Сжать прозу в `src/` (≈27 %): историю перенести в git/docs, в коде оставить инварианты | §5.8, §10 |
| 4 | DEC-01 | P1 | Разделить `tasks.py`: чистые помощники → `board/card_rows.py`; диспетчерские записи → mixin. Сейчас 51 приватный импорт из `ummanu.tasks` | §5.1 |
| 5 | DEC-02 | P1 | Разделить `dispatch/host.py` на mixin-модули. Именно mixin: тесты патчат методы через `self` | §5.2 |
| 6 | DEC-05 | P1 | `runtime/local_pty_head.py` → пакет из 5 модулей; ридеры журнала и аренды — к `journal.py` | §5.5 |
| 7 | DEC-07 | P1 | Вынести CSS/JS (≈58 тыс. символов, 23 %) из `web/pages.py` в package-data и разбить страницы на модули | §5.7 |
| 8 | TEST-01 | P1 | Разделить `test_dispatcher.py`; снять Docker-гейт с 12 классов без Postgres; урезать фикстуру на 1 440 строк до 40 | §5.9, §6.4 |
| 9 | DOC-01 | P1 | Разбить `PROTOCOLS.md` (365 тыс.) и `OPERATIONS.md` (220 тыс.) на тематические файлы; удалить закрытую историю (≈90–100 тыс.) | §8 |
| 10 | CI-01 | P1 | Добавить в CI ruff (check по изменённым файлам), pip-cache, `concurrency`, `permissions` | §8.4 |
| 11 | DEAD-T | P2 | Вывести из эксплуатации одноразовую миграцию `transition/` и скрипты переименования (≈159 тыс. символов в src+scripts и 83 тыс. в тестах). Нужно решение владельца | §6.2 |
| 12 | CON-01 | P2 | Две вымышленные/вымершие части схемы инстанса: блок `heads` (ломает `apply_host`) и `orca_repos` | §7 |
| 13 | CON-02 | P2 | Ответ `product_runs` не проходит собственную схему `web-run`; у ряда JSON-маршрутов схемы нет вовсе | §7 |
| 14 | ARCH-04 | P2 | `board` зависит от верхнего слоя (`tasks`, `sprints`, `product_issues`), в том числе через приватные имена | §4, §5.3 |
| 15 | INEF-01 | P2 | `DispatcherRuntime.save_records` на каждое сохранение делает чтение доски и ≈6 `git rev-parse` на каждую активную запись; 144 точки вызова | §9 |

## 2. Инвентаризация модулей и матрица покрытия

### 2.1 Размеры пакетов `src/ummanu`

Измерено по `git ls-files`; «проза» = комментарии + docstring как доля символов (**Ф**).

| Пакет | Файлов | Строк | Символов | ≈Токенов | Проза |
|---|---|---|---|---|---|
| `dispatch` | 58 | 44 216 | 1 967 853 | 491 963 | 27 % |
| корень `ummanu/*.py` | 57 | 44 641 | 1 911 500 | 477 875 | 18 % |
| `board` (без миграций) | 57 | 18 427 | 800 494 | 200 123 | 21 % |
| `webproto` | 30 | 13 089 | 628 604 | 157 151 | 47 % |
| `runtime` (без `head/`) | 28 | 8 709 | 406 945 | 101 736 | 44 % |
| `web` | 10 | 8 189 | 397 280 | 99 320 | 24 % |
| `runtime/head` (+ `local_pty`) | 21 | 6 656 | 297 625 | 74 406 | 35 % |
| `automations` | 30 | 5 687 | 243 869 | 60 967 | 31 % |
| `po` | 13 | 4 255 | 191 001 | 47 750 | 27 % |
| `schemas` (JSON) | 16 | 5 122 | 178 368 | 44 592 | — |
| `transition` | 13 | 3 282 | 143 658 | 35 914 | 13 % |
| `infra` | 15 | 2 916 | 124 727 | 31 181 | 19 % |
| `board/migrations` | 34 | 2 791 | 123 249 | 30 812 | 25 % |
| `memory` | 8 | 1 892 | 73 980 | 18 495 | 16 % |
| `webfront` | 4 | 1 026 | 42 191 | 10 547 | 28 % |
| `projects` | 4 | 642 | 30 595 | 7 648 | 53 % |

### 2.2 Крупнейшие файлы `src/`

| Файл | Строк | Символов | Проза | Крупнейшая функция или класс |
|---|---|---|---|---|
| `tasks.py` | 5 934 | 285 875 | 19 % | `TaskWriter` (1105–5772, 4 668 строк); `_create` — 495 строк (`:1260`) |
| `web/pages.py` | 4 996 | 250 050 | 16 % (+23 % CSS/JS, +11 % HTML-литералы) | `po_session` — 145 |
| `dispatch/host.py` | 5 004 | 240 543 | 24 % | `CommandHostRuntime` (837–4857); `_worker_task_doc` — 300 |
| `runtime/local_pty_head.py` | 3 445 | 180 579 | 52 % | `LocalPtyHeadRuntime` — 1 983 |
| `sprints.py` | 3 824 | 174 100 | 18 % | `SprintWriter` — 2 691 |
| `dispatch/observer.py` | 3 603 | 159 617 | 32 % | `_reconcile_open_sprint` — 363; `_launch_observer` — 345 |
| `webproto/sprint_reads.py` | 2 773 | 135 048 | 42 % | `SprintSections.waiting` — 117 |
| `upgrade.py` | 2 852 | 128 709 | 21 % | `step_web` — 137 |
| `checkpoint.py` | 2 617 | 119 950 | 19 % | `checkpoint_snapshot` — 129 |
| `installation.py` | 2 349 | 105 107 | 17 % | `install` — 349 |
| `cli.py` | 2 265 | 90 041 | 9 % | `build_parser` — 350 |
| `dispatch/production.py` | 1 951 | 83 224 | 25 % | `_reconcile_production` — 149 |
| `dispatch/e2e_after_merge.py` | 1 749 | 82 141 | 14 % | `_batch_decision` — 108 |
| `dispatch/cleanup.py` | 1 421 | 78 552 | 10 % | — |
| `restore.py` | 1 676 | 72 934 | 13 % | 22 функционально-локальных импорта |
| `dispatch/head_vitality_episode.py` | 1 293 | 70 927 | 43 % | **`reduce_vitality` — 501** (самая длинная функция в репозитории) |
| `dispatch/state.py` | 1 435 | 68 322 | 31 % | `DispatcherRecord.from_json` — 147 |
| `web/app.py` | 1 421 | 67 970 | 36 % | — |
| `dispatch/gate.py` | 1 401 | 65 358 | 38 % | — |
| `board/sql_host.py` | 1 451 | 64 904 | 7 % | `_transition_issue` — 104 |

Другие функции длиннее 200 строк (**Ф**, по `ast`):

| Функция | Место | Строк |
|---|---|---|
| `0001_initial.upgrade` | — | 422 (заморожена, не трогать) |
| `_wake_for_event` | `observer.py:1795` | 251 |
| `start_review` | `review.py:849` | 246 |
| `_prepare_claim` | `claim.py:542` | 245 |
| `WorkerContinuationLiveness.from_json` | `worker_lifecycle.py:316` | 241 |
| `_deliver_red_continuation` | `worker_continuation.py:256` | 227 |
| `launch_worker_after_claim` | `worker_launch.py:178` | 223 |
| `restore_cards_batched` | `task_restore.py:130` | 210 |
| `_decide_wait_by_verdict` | `wait_vitality.py:158` | 279 |
| `add_task_subcommands` | `task_commands.py:86` | 269 |

### 2.3 Матрица покрытия

Глубина: **П** — прочитано полностью; **Ч** — существенная часть плюс полный AST-контур; **А** — автоматические проходы (метрики, ссылки, клоны, ruff) и чтение по наводкам.

| Компонент | Файлы и точки входа | Глубина | Что сделано |
|---|---|---|---|
| `dispatch` A | `host.py`, `observer.py`, `state.py`, `commands.py`, `types.py`, `heartbeat.py`, `headless.py`, `bootstrap.py`, `decision_pointer.py`, `git_workspace.py` | П | Полный контур классов; дубли; мёртвые re-export |
| | `production.py`, `claim.py`, `worker_launch.py`, `worker_lifecycle.py`, `runtime.py`, `worker_report.py`, `worker_continuation.py`, `runtime_preflight.py`, `helpers.py`, `launch.py` | Ч | |
| | `tui.py`, `provider_failure.py`, `origin_returns.py`, `observer_fence.py`, `watchdog.py`, `launcher.py`, `worker_comments.py`, `production_checkout.py`, `entrypoint_guard.py`, `runtime_provenance.py` | А | |
| | Точка входа `dispatcher production-tick` (systemd → `runtime_preflight.py` → CLI) | | Контракт сверен |
| `dispatch` B | `head_vitality*.py`, `wait_vitality.py`, `review.py`, `gate_lifecycle.py`, `head_status.py`, `review_verdict.py`, `gate_attestation.py`, `dispatch/gate.py` | П | Сверка с `HEAD_VITALITY.md`, `OWNED_CLEANUP.md`, `OUTCOME_LINEAGE.md` |
| | `e2e*.py`, `cleanup.py`, `release_lifecycle.py`, `pause_ops.py`, `assessment_decision.py`, `attempt_*.py`, `gate_receipt.py`, `pause.py` | Ч | |
| | `post_merge.py`, `wait_cards.py`, `po_cards.py`, `po_delivery.py`, `release_activation.py` | А | |
| Ядро (корень) | `runtime_env`, `_proc`, `_fsutil`, `cli_output`, `observer_root`, `__init__`, `__main__` | П | Полное дерево argparse: 150 путей команд; 188 вызовов из `PROTOCOLS.md`/skills проверены на флаги |
| | `tasks.py` (≈60 %), `sprints.py` (≈40 %), `cli.py`, `task_commands.py`, `sprint_commands.py`, `product_issue_commands.py`, `product_issues.py` | Ч | |
| | `check_commands`, `broad_check`, `status`, `session`, `routing_journal`, `candidate_history`, `codex_provider_events`, `knowledge_write`, `onboarding`, `role_skills`, `config`, `data`, `product_lanes`, `sprint_observer`, `sprint_close` | А | |
| Установка и восстановление | `upgrade.py`, `installation.py`, `checkpoint.py`, `restore.py`, `secret_store.py`, `backup*.py`, `state_repo.py`, `host.py`, `host_apply.py`, `memory_service.py`, `memory/canon.py`, `memory_write.py`, `memory_journal.py`, `memory/access.py`, `infra/{instance_maintenance,live_root_findings,snapshot_tree,env,host_space_policy}.py`, `transition/{names,commands,rewrite}.py`, `restore_commands.py` | Ч | Все схемы; примеры провалидированы в памяти; сверка с `RECOVERY.md`, `RENAME.md`, `SECURITY.md` |
| | Остальные `infra/*`, `memory/*`, `secret_*`, `host_commands`, `backup_retention`, `memory_reindex` | А | |
| | `transition/{steps,rollback,board,preconditions}.py` | А | Предложены к выводу целиком, построчно не читались |
| `board` | `backend.py`, `host.py`, `fake.py`, `sql_host.py`, `sql_cards.py`, `sql_sprints.py`, `sql_product_issues.py`, `sql_audit.py`, `schema.py`, `models.py`, `migrations/env.py`, миграции 0001/0004/0006/0007, `legacy_codec.py`, `done_retention.py`, `owner_event_commands.py`, `extension_bag.py` | П | Все 30 миграций отрендерены в SQL офлайн и сверены с `schema.metadata`. Сверка с `BOARD_STORE.md` |
| | Остальные 34 модуля | Ч/А | |
| `web`, `webproto`, `webfront` | `app.py`, `server.py`, `caddyfile.py`, ключевые части `pages.py`, `sprint_reads.py`, `pause_reads.py`, `command_reads.py`, `reads.py`, `ops.py`, `sprint_ops.py`, `card_ops.py`, `pause_ops.py`, `runs.py`, `sprint_requests.py`, `sources.py`, `cursor.py`, `boundary.py`, `errors.py`, `owner_events.py`, `store_io.py` | П/Ч | 43 маршрута `ROUTES` сверены с таблицей `PROTOCOLS.md`. Все 5 схем `web-*`. Smuggling воспроизведён на живом `WebServer` |
| | `lifecycle.py`, `run_state.py`, `head_view.py`, `admission.py`, `po_ops.py`, `journal.py`, `section.py`, `agents.py`, `run_events.py`, `provider_ops.py`, `workspaces.py`, `po_auth.py`, `web/health.py`, `web/commands.py`, внутренности `guard.py` | А | |
| `runtime`, `head`, `local_pty` | `local_pty_head.py`, `head/runtime.py`, `heads.py`, `head_runtimes.py`, `head_runtime_backends.py`, `head_run_binding.py`, `shared_state.py`, `container_labels.py`, `launch_prefix.py`, `heads.toml`, `docker-bin/docker`, `supervisor.py`, `client.py`, `protocol.py`, `journal.py` | П | Импорт-стоимость supervisor измерена. Сверка с `HEAD_RUNTIME.md` |
| | `state.py`, `tui_delivery.py`, `agent_prompt_transport.py`, `identity.py`, `command.py`, `operations.py`, `task_ref.py`, `scoped_lifecycle.py`, `codex_home.py`, `codex_preflight.py` | Ч | |
| | Остальные модули | А | |
| `po`, `projects`, `automations` | `automations/runtime/dispatch.py`, `agents/pipeline/*`, `automations/__main__.py` | П | |
| | `po/service.py`, `po/runner.py`, `po/store.py`, `steward/signals.py`, `curator/discover.py` | Ч | |
| | Остальные (`projects/*`, `curator/*`, `retro/*`, `steward/cli.py`, `automation.toml`) | А | |
| Тесты | Все `tests/**/*.py` | А | Инвентарь классов и тестов, AST-клоны, окна-клоны, мёртвые помощники, skip-декораторы, повторный запуск унаследованных тестов, доля прозы, ruff |
| | `README.md`, `__init__.py`, `broad.py`, `ci-shards.txt`, `dispatcher_fixtures.py`, `support/git.py`, `web_fakes.py`, `test_hermetic_board.py` | П | |
| | Десятки диапазонов крупных файлов | Ч | |
| | Ссылки на все `tests/fixtures/*` | | Проверены |
| Документация и skills | `README`, `CONTRIBUTING`, `SECURITY`, `TESTING.md`, `OWNED_CLEANUP.md`, `skills/README.md`, `manifest.toml`, 3 `AGENTS.md`, `codex-home/config.toml`, 18 юнитов systemd, `examples/instance/*`, `pyproject.toml`, оба workflow, `.gitignore` | П | Вычислены дубли (шинглы), плотность кодовых символов, «исторические» маркеры, якоря. Проверено существование путей, модулей и тестов |
| | Остальные `docs/*.md`, тела `SKILL.md` | Ч | |
| `scripts/` | Все 10 скриптов | Ч/А | Заголовки; ссылки из CI, docs и тестов |

## 3. Текущая архитектура и карта контрактов

### 3.1 Слои и потоки

```text
systemd timers/services (packaging/systemd, рендер infra/systemd.py + host_apply.py)
   ├─ ummanu-dispatcher-production.timer (60s) ─► dispatch/runtime_preflight.py (stdlib-only) ─► `ummanu dispatcher production-tick`
   │        └─ dispatch/production.py → claim / worker_launch / review / gate_lifecycle / e2e_* / wait_vitality / observer …
   │              ├─ runtime/head_runtime_backends → runtime/local_pty_head (backend) → runtime/head/local_pty/{client,supervisor,journal,protocol}
   │              ├─ tasks.TaskWriter / sprints.SprintWriter / product_issues (протокол записи) → board/* (SQLAlchemy + psycopg, Postgres 16, Alembic 0001…0030)
   │              └─ dispatch/state.py: production-state.json (DispatcherRecord, ObserverRecord) в data dir
   ├─ ummanu-curator/steward/retro.timer ─► scripts/ummanu-agent-gate.sh ─► `ummanu automations <agent> …` (automations/composition.py)
   ├─ ummanu-web.service ─► `ummanu web-serve` (web/server.py → web/app.py → webproto/* → board/state) ; ummanu-web-front ─► Caddy (webfront/caddyfile.py, guard.py)
   ├─ ummanu-po.service ─► `ummanu po-serve` (po/service.py, Unix socket, po/store.py → Postgres)
   ├─ ummanu-memory.service ─► `ummanu-memory-mcp` (memory_service.py, MCP, sqlite-vec + fastembed)
   ├─ ummanu-doctor.timer ─► `ummanu doctor-record` ; ummanu-instance-maintenance.timer ─► `ummanu instance-maintenance`
CLI (cli.py + *_commands.py) — оператор и головы агентов: task / sprint / product / issue / check / data / backup / restore / secret / upgrade / install / recover …
Установка и восстановление: bootstrap.py → installation.py (install / recover) → checkpoint.py (экспорт снапшота в отдельный git repo, push в instance remote) ↔ restore.py / task_restore.py ; upgrade.py (26 шагов)
```

### 3.2 Карта контрактов: кто пишет и кто читает

| Контракт | Производитель | Потребители | Форма | Замеченные расхождения |
|---|---|---|---|---|
| Таблицы доски (30 таблиц) | `board/schema.py` + миграции | `board/sql_*`, `tasks`, `sprints`, `po/store`, `webproto` | SQL | Схема и миграции совпадают (**Ф**). В `BOARD_STORE.md` 12 расхождений (§8). `board_events.committed` всегда `true` и не читается. Колонки `tasks.claimed_at`/`resolved_*_family` никогда не пишутся |
| Словарь карточек (`CardState`, kinds, маркеры) | `board/models.py`, `transitions.py` | dispatch, webproto, web | enum и литералы | ≈66 строковых литералов состояний вне `board` против 8 использований `CardState.`. Словари issue/маркеров заданы трижды (CON-05) |
| `production-state.json` (`DispatcherRecord`, `ObserverRecord`) | `dispatch/state.py`, `observer.py` | dispatch, `webproto/reads.py`, `status.py`, `cli` | JSON | 8 одинаковых обёрток `Persisted*`. Состояние наблюдателя `"idle-recovering"` проверяется, но нигде не присваивается. Две формы имени роли: `"review"` и `"reviewer"` |
| Исходы шага тика (`{"status": …, "to": …}`) | `gate_lifecycle`, `wait_vitality`, `worker_report`, `worker_launch` | `production.py:1225` (fence) | dict | Блокировка возвращается как `"status":"ok"`, и fence её не видит (CON-03, **Ф ✔**) |
| Request id протокола | `watchdog.py:281`, `wait_vitality`, `review.py` | `claim.py:626-651` | строка | 4 из 21 действий получают суффикс цикла, и поиск в claim по ним никогда не совпадает (CON-04) |
| Журнал головы и протокол сокета | `head/local_pty/supervisor.py`, `journal.py` | `client.py`, `local_pty_head.py`, `po/runner.py` | JSONL + Unix socket | Неиспользуемые `OP_RESIZE`/`OP_ATTACH`/push-события. Неверный комментарий про запись «< 256 байт». Журнал без ротации (INEF-05) |
| `head.pid` (launch identity) | heartbeat-обёртка головы | `client._identity_written`, `local_pty_head._identity_says_dead` | JSON | Файл не удаляется между инкарнациями с тем же `run_id` (BUG-11, **Г**) |
| JSON-схемы `schemas/*.json` | `config.py`, `webproto/*`, `gate.py`, `provision.py` | валидаторы, тесты | JSON Schema | `web-run` не принимает `product_runs` (**Ф**). У ряда маршрутов нет схемы. `instance.heads`/`orca_repos` устарели. `data-manifest.components` пишется, но не читается |
| HTTP API (43 маршрута) | `web/app.py ROUTES` | браузер, Caddy, тесты | HTTP + JSON | Таблица маршрутов совпадает с `PROTOCOLS.md` (**Ф**). Коды ошибок «неверный конфиг инстанса» расходятся: 503 против 400 (CON-07) |
| Чекпоинт и снапшот | `checkpoint.py` (exporter) | `restore.py`, `installation.recover` | git-дерево плюс manifest | Унаследованный режим «live root — git work tree» ещё живёт в коде (≈450 строк), хотя по документации запрещён (ARCH-09) |
| `runtime.env` | установщик, оператор | `runtime_env.read_runtime_env`, `runtime/role_env.load_env_file`, `secret_store` | `KEY=VAL` | Три парсера с разной семантикой кавычек и `export` (CON-09) |
| Факты памяти (frontmatter) | `memory_write._split_fact` | `memory/canon.parse_frontmatter`, `memory_journal` | Markdown + YAML | Парсеры расходятся (BUG-09, **Ф ✔**) |
| Skills и prompts | `skills/manifest.toml`, `role_skills.py` | Claude/Codex-головы | `SKILL.md` | Codex-skills доставляются в старый Orca `CODEX_HOME` (DOC-06, **Г** по влиянию) |
| Юниты systemd | `packaging/systemd/*` | `host_apply`, `upgrade` | шаблоны | Все `ExecStart` существуют (**Ф**). Steward: в skill написано «hourly», таймер — раз в 3 часа (DOC-05) |

## 4. Находки: архитектура и сопровождаемость

Баги и очевидные неэффективности собраны отдельно, в §9.

Обозначения в столбцах:
- Приоритет: P0 — срочно, P1 — высокий, P2 — средний, P3 — низкий.
- Риск изменения и трудоёмкость: S — малый, M — средний, L — большой.
- Метки достоверности те же, что в §1: Ф, О, Г, ✔.

| ID | Приор. | Компонент | Доказательство | Эффект | Риск | Трудоёмк. | Метка |
|---|---|---|---|---|---|---|---|
| ARCH-01 | P1 | весь `src/` | Комментарии и docstring — 26,7 % символов. Худшие файлы: `local_pty_head.py` 52 %, `head_vitality_policy.py` 56 %, `head_vitality_guard.py` 52 %, `head_health.py` 51 %, `webproto/lifecycle.py` 57 %, `dispatch/types.py` 70 %, `projects/contract.py` 54 %. Ссылки на карточки и инциденты: 74 в зоне dispatch A, 51 в ядре задач, 71 в web | −450…650 тыс. символов в `src` | нулевой для runtime; тесты, которые ищут текст в исходниках, — см. §10 | M–L, механически, по файлам | Ф/О |
| ARCH-02 | P1 | `tasks.py` | 51 импорт приватных имён `ummanu.tasks` из других модулей (`sprints.py:89-101`, `product_issues.py:19-28`, `task_restore.py` — 14 имён, `board/sql_host.py:45-55`) | `tasks.py` стал библиотекой утилит: любому потребителю `_text`/`_digest` приходится открывать 286 тыс. символов | S | S | Ф |
| ARCH-03 | P2 | весь `src/` | 336 функционально-локальных импортов. `task_restore.py`: 23 из 24 не нужны — `import ummanu.tasks` не загружает `task_restore` (проверено по `sys.modules`). В `po_cards.py:158-236` 6 локальных импортов модулей, уже импортированных в шапке (`:90-92`). Повторные локальные импорты в `host.py:1827,2422,3027,3845`, `local_pty_head.py:3320,3372`, `po/service.py:901` | Шум и ложные сигналы «здесь цикл» | S (каждый проверять тестом импорта) | S | Ф |
| ARCH-04 | P2 | `board` | `board` импортирует `tasks`/`sprints`/`product_issues` (`sql_host.py:45-55`; `done_retention.py:9`, `steward_reports.py:8`, `reference_repair.py:15`, `normalized_checkpoint.py:10`). Ещё 14 локальных `from ummanu.tasks import TaskError` | Инверсия слоёв, циклы | S: перенести `TaskError` (`tasks.py:159`, 8 строк) в `board/errors.py` и реэкспортировать | S | Ф |
| ARCH-05 | P2 | `board` | Модули не в своём пакете: `owner_event_commands.py` (CLI), `done_retention.py`, `steward_reports.py` (их импортирует только `automations/composition.py`), `import_order.py` (импортирует только `task_restore.py`) | ≈14 тыс. символов лишнего контекста `board` | S | S | Ф |
| ARCH-06 | P2 | `pyproject.toml` / ruff | Ruff isort без `combine-as-imports`: каждый `as`-импорт отдельным оператором. `host.py` — 123 строки, `runtime.py` — 107, `worker_launch.py` — 57; в зоне dispatch B 655 строк импортов | ≈−400…500 строк, чисто механически | нулевой | S | Ф/О |
| ARCH-07 | P2 | `dispatch/runtime.py`, `host.py` | 31 «compatibility re-export» (`runtime.py:20-22,41-64,155-165`; `host.py:166-167`). AST-скан всех импортов и атрибутных обращений в src/tests/scripts не нашёл ни одного потребителя | ≈−45 строк, меньше ложных путей | нулевой | S | Ф |
| ARCH-08 | P2 | dispatch | 13 импортов приватных имён `dispatch/gate.py` из e2e, e2e_stage, e2e_after_merge, post_merge, wait_cards, gate_lifecycle (`_rollup`, `_backend_call`, `_failed_log`, `_gh_api`, `_HTTP_STATUS_RE`, `_LogFragment`, `_fingerprint`) | Фактически публичный API GitHub CI спрятан в `gate.py` | S | M | Ф |
| ARCH-09 | P2 | установка | Унаследованный режим «live root — git work tree»: неиспользуемые методы `CheckpointWriter` (164 строки), клонирование в `installation.py:294-467`, `state_repo.PACKING_CONTROLS`, `upgrade.step_instance_packing`. `ARCHITECTURE.md:108` такой режим запрещает, но `RECOVERY.md:714-716` обещает восстановление с legacy-tip | ≈−450 строк | M–H (нужно решение владельца) | M | Ф |
| ARCH-10 | P2 | `runtime` | Остатки Orca-эпохи: pane-словарь `OBSERVE_PANE_*` (`head/runtime.py:66-70`), который единственный backend не порождает, хотя на него ветвится `host.py:1580-1583`. Pane-методы `AgentState`; `agent_prompt_transport` (используются 2 константы из 107 строк); модуль `pipeline/codex_sessions.py` нужен только тестам; умолчание `~/orca/workspaces` встречается в 5 местах | ≈−22 тыс. символов кода плюс шум | S | S–M | Ф |
| ARCH-11 | P2 | `head/local_pty/supervisor.py` | Импорт supervisor тянет 44 модуля `ummanu`, из них 13 `board.*`. Цепочка: `head/__init__` → `command` → `role_env` → `docker_guard` → `board.local_run`. Это ≈225 мс и +10 МБ RSS на каждую живую голову | Память и время старта на каждую голову | S: `with_pid_heartbeat` вынести в лист, реэкспорт в `head/__init__` сделать ленивым (PEP 562) | S | Ф (измерено) |
| ARCH-12 | P3 | `cli.py` | Любой вызов `ummanu …`, включая частый `task show`, импортирует 275 модулей, ≈0,64 с по `-X importtime`. `web/server` тянет ≈508 модулей | Латентность каждого вызова CLI агентами | M | M | Ф (изм.) / Г (выигрыш) |
| ARCH-13 | P2 | `board/fake.py` | `FakeBoardHost`/`MemoryAudit` (16 тыс. символов) лежат в `src`, но используются только тестами; `MemoryAudit` скопирован ещё в 2 тестовых файла. Fake расходится с SQL по семантике (§7, CON-12) | −16 тыс. символов из `src` | S (убрать из `__all__`) | S | Ф |
| ARCH-14 | P3 | имена модулей | Повторяющиеся basename: `sprint_close.py` ×2, `sprints.py` ×2, `host.py` ×3, `gate.py` ×2 (верхний уровень — онбординг, `dispatch/` — CI), `commands.py` ×6, `provision.py` ×2 | Агент открывает не тот файл | S (переименование с shim) | S | Ф |
| ARCH-15 | P2 | dispatch | 17 «утиных» проб `getattr(runtime.host, "...", None)` под тестовые фейки (`observer.py:1640,1859,2059,2778`, `worker_launch.py:284`, `host.py:439,1487,2210,2782` и др.). У реального host эти атрибуты есть всегда | Мёртвые fallback-ветки, неуверенность при чтении | M (фейки придётся дополнить) | M | Ф/Г |
| ARCH-16 | P2 | web | Два противоположных правила перехвата ошибок источников: фиксированный кортеж `SOURCE_FAILURES` (`reads`, `sprint_reads`) и «span ловит всё» (`pause_reads`, `command_reads`). Следствие — BUG-07 | Нестабильная деградация дашборда | S | S | Ф |
| ARCH-17 | P3 | `po/service.py` | `PoService.pump` держит `_lock` сервиса и `_lock` runner во время `spawn_head` (до 20 с + systemd-run), а `submit()` вызывает `pump()` прямо в своём потоке | Сериализация всех PO-операций | M | M | Ф/Г (влияние) |
| CI-01 | P1 | `.github/workflows/ci.yml` | Нет ни одного шага ruff. Нет `cache: pip`. Нет `concurrency` и `permissions`. 9 suite-джобов ставят `.[memory,ci]` (fastembed/onnxruntime) даже для `unit` | Нет гейта линтера; лишние минуты CI | S | S | Ф |
| TEST-01 | P1 | tests | `test_dispatcher.py`: 725 тыс. символов; `setUpModule:195` требует Docker для всех 27 классов, хотя 12 из них Postgres не используют (например, `DispatcherGateTests` — 86 тестов, только git) | Самый большой файл ≈181 тыс. токенов; лишний Docker-шард | S–M | M | Ф |
| TEST-02 | P2 | tests | 38 копий `git(cwd,*args)` при готовом `tests/support/git.py:7`. 8 копий `_dead_pid` с двумя стратегиями. 7 копий `_alive`, 5 — `_kill`. 20 литералов `"version: 1\nname: test\n"`. 8 копий setUp диспетчера | ≈1 400 строк повторов | S | M | Ф |
| DOC-01 | P1 | docs | `PROTOCOLS.md` 365 тыс., `OPERATIONS.md` 220 тыс. — монолиты. ≈90–100 тыс. символов закрытой истории. Разделы дописываются в конец файла вне структуры (`PROTOCOLS.md:4710-4964`, `BOARD_STORE.md:1297`, `OPERATIONS.md:3199`) | −30 % объёма docs; −80…95 % контекста на один вопрос | M (37 тестов читают текст docs; рантайм цитирует заголовки) | L | Ф/О |

## 5. Предложения по декомпозиции гигантских файлов, классов и функций

Общая методика оценки контекста:
- 1 токен ≈ 4 символа.
- «До» — сколько символов агенту нужно загрузить для типовой правки, если сейчас он открывает весь файл.
- «После» — целевой модуль плюс его прямые типы.
- Погрешность ±20–30 %, потому что границы задач условны.

Разбиение почти никогда не уменьшает общий объём: заголовки импортов добавляют +1–2 %. Выигрыш — в контексте.

### 5.1 `src/ummanu/tasks.py` (5 934 строки, 286 тыс. символов) — DEC-01

**Сейчас.** `TaskWriter` (1105–5772) объединяет девять кластеров:

| Кластер | Строки | Символов |
|---|---|---|
| Ошибки, git-грязь, request id, предикаты событий | 159–702 | 24,0 тыс. |
| `TaskReader` | 704–1102 | 20,3 тыс. |
| Создание карточки | 1181–1916 | 35,7 тыс. |
| Комментарий, владелец, отчёт, завершение, передача, отмена | 1918–2493 | 28,7 тыс. |
| Записи только для диспетчера: wait, PO, e2e, after-merge, routing, usage, outcome | — | 51,0 тыс. |
| claim, move, transition, decide, verdict | — | ≈35 тыс. |
| Охрана спринта | 4275–4764 | 21,9 тыс. |
| Архив, retire, restore | 4766–4985 | 10,5 тыс. |
| Движок записи и reconcile/pending | 4987–5772 | 39,1 тыс. |

`grep` подтверждает, что 20 методов (`record_*`, `after_merge_*`, `routing`, `attempt_*`, `post_merge_ci`, `escalate_po_card`, `settle_cleanup_claim`, `open_sprints_reserving`) вызываются только из `src/ummanu/dispatch/` (**Ф**).

**Предлагаемое разбиение.**

1. `board/card_rows.py`: чистые помощники строк и кодеков — `_digest`, `_now`, `_rfc3339`, `_task_number`, `_task_is_active`, `_task_metadata`, `_matching_swimlane`, `_target_column_id`, `_normalize_comment`, `all_project_cards`, `project_card_by_*` (≈6 тыс. символов). Рядом `board/card_events.py`: предикаты событий из строк 219–640 (≈21 тыс.). В `tasks` имена остаются привязанными через `from … import …`, потому что тесты патчат `ummanu.tasks._task_number` (4 раза), `specification_revision` и `workspace_dirt`.
2. `task_dispatcher_writes.py`: класс `DispatcherTaskWrites` (mixin, ≈51 тыс.).
3. `task_sprint_guard.py`: `SprintGuardMixin` (≈22 тыс.).
4. `task_recovery.py`: `RecoveryMixin` (≈25 тыс.).
5. Итог: `class TaskWriter(DispatcherTaskWrites, SprintGuardMixin, RecoveryMixin)`. Ядро `tasks.py` сжимается примерно до 150 тыс. символов.

**Интерфейс.** Публичные пути импорта не меняются. Mixin сохраняет `self`, поэтому `patch.object(writer, …)` продолжает работать.

**Контекст.**

| Задача | До | После | Токенов |
|---|---|---|---|
| Правка помощника | 286 тыс. | ≈27 тыс. | −65 тыс. |
| Правка диспетчерской записи | 286 тыс. | ≈51 тыс. плюс ядро-интерфейс ≈20 тыс. | ≈−54 тыс. |

**Чистое сокращение** даёт только сопутствующая чистка: DUP-C4 (≈1,9 тыс.), `create` пробрасывает 32 kwargs в `_create` (`tasks.py:1181-1295`, ≈2,8 тыс.), DUP-C7, мёртвые алиасы DEAD-15. Итого −6…8 тыс. символов (**О**).

**Альтернатива.** Композиция (`writer.dispatch.record_x`) чище, но ломает патчи в тестах и сигнатуры у вызывающих в `dispatch`. На первом шаге не рекомендуется.

### 5.2 `src/ummanu/dispatch/host.py` (5 004 строки, 240 тыс.; `CommandHostRuntime` ≈175 методов) — DEC-02

**Кластеры** (строки):

| Строки | Содержимое |
|---|---|
| 1–320 | Импорты |
| 408–752 | `InstanceCatalog` |
| 885–1045 | Ingress провайдера и preflight |
| 1047–1172 | Подъём worker |
| 1174–1752 | Наблюдатель |
| 1754–1847 | Прогресс и отказ провайдера |
| 1849–1998 | Ревью |
| 1999–2360 | Git: reconcile, gate, merge |
| 2362–2796 и 3535–3627 | Stop, signal, retain головы |
| 2798–3105 | Seed workspace и Python-окружение |
| 3107–3533 | `_launch` и резолвер runtime |
| 3545–3840 | Continuation и nudge |
| 3842–4742 и 4876–5004 | Prompt-документы |
| 4744–4873 | Удалённый git и запуск процессов |

**Разбиение через mixin.** Тесты делают `mock.patch.object(host, "_worker_task_doc")`, `"_launch"`, `"_run"`, `"run_capture"`, `"head_runtime_for"` (например, `tests/test_dispatcher_runtime_isolation.py:603`). Mixin сохраняет поиск через `self.`, поэтому эти патчи продолжат работать.

| Новый модуль | Что переходит | Объём |
|---|---|---|
| `dispatch/task_documents.py` (`TaskDocumentMixin`) | Prompt-рендеринг, чистый; заодно вынести постоянную прозу prompt в константы | ≈950 строк / 45 тыс. |
| `dispatch/host_observer.py` | Наблюдатель | ≈580 строк |
| `dispatch/host_heads.py` | Stop/signal/retain | ≈520 строк |
| `dispatch/host_git.py` | Git-операции | ≈550 строк |
| `dispatch/workspace_env.py` | Seed и окружение | ≈240 строк |
| `dispatch/host_catalog.py` | `InstanceCatalog`, `live_root_project_refusal`, `_same_repo`; меняются импорты в `bootstrap.py` и `claim.py` | ≈400 строк / 19 тыс. |

После разбиения в `host.py` остаётся ≈1 300 строк.

**Сопутствующее сокращение** (≈−250 строк, **О**):
- §6.3 DUP-D4…D7: двойники worker/reviewer, `_head_status` ×5, дубль preflight в `_launch` (3134–3147 ≡ 3162–3175);
- `combine-as-imports` (−123 строки);
- раздел «No subagents» встречается в prompt трижды: `host.py:4202-4206`, `4610-4614`, `observer.py:3368-3371`.

**Контекст.** Задача «поменять текст TASK.md»: сейчас ≈60 тыс. токенов (весь файл) или ≈20 тыс., если собирать вручную по диапазонам. После разбиения ≈11 тыс., а после сжатия docstring ≈8 тыс. (**О**, ±30 %).

### 5.3 `board/sql_host.py` (65 тыс.) и `board/sql_cards.py` (57 тыс.) — DEC-03

`sql_host.py` состоит из трёх кластеров:

| Кластер | Символов | Что входит |
|---|---|---|
| Product/Issue | ≈26,6 тыс. | `create`, `replace`, `_append_description`, `recover_product_issue`, `_transition_issue` и 15 помощников из 872–1100 |
| Card | ≈18,4 тыс. | — |
| Sprint | ≈9,8 тыс. | 487–870 |

**Разбиение.** Product/Issue → `sql_host_records.py`, Sprint → `sql_host_sprint.py`. `SqlBoardHost` остаётся единственным классом, который импортируют снаружи. Для работы с Product/Issue это даёт −54 % контекста (30 тыс. вместо 65 тыс.).

В `sql_cards.py` пул соединений и транзакции (152–638, ≈20,9 тыс.) не зависят от словаря доски (640–1306, ≈29,6 тыс.). Предложение: `sql_pool.py` с базовым классом `_PooledClient`; имя `SqlCardClient` сохраняется. Перед разбиением проверить, что тесты не патчат `_borrow`/`_session` на `SqlCardClient`.

**Чистое сокращение** (≈−300 строк / −12 тыс., **О**):
- общий лист `board/sql_rows.py` для `_now`/`_epoch`/`_rfc3339`/`_grouped`/`SqlCardError` (§6.3 DUP-B1);
- `event_id()` (повторяется 5 раз);
- `_has_comment()` (4 раза);
- сборка board-row dict (6 мест);
- одна спецификация метаданных карточки вместо `_METADATA_*`, кортежа `names` и SELECT-списка. Их объединение совпадает с `legacy_codec.TASK_KNOWN_METADATA`, симметричная разность пуста (**Ф**).

Ещё `transitions.py:79-202`: таблицу из 42 рёбер (124 строки) можно генерировать циклом с умолчанием и 15 исключениями (≈25 строк, −4 тыс.). Старый и новый dict сравнить тестом.

### 5.4 `dispatch/observer.py` (3 603 строки, 160 тыс.) — DEC-04

**Чистые разрезы:**
- `observer_state.py`: `ObserverDelivery`/`ObserverRecord`, JSON, load/put, snapshot (98–573, ≈475 строк);
- `observer_prompt.py`: 3336–3592, чисто;
- `observer_audit.py`: 3212–3333; общий `_persist_quietly` с `launch.py:1236`.

**Связанное ядро.** Delivery и launch остаются вместе: `_fail_delivery` и `_replace_observer_for_no_progress` вызывают `_launch_observer` (`:1486`, `:1700`), а тот вызывает defer/stop. Если их разнести, вернутся локальные импорты. Тесты патчат `ummanu.dispatch.observer._launch_observer` и `_reconcile_open_sprint` строкой, поэтому вызывающие должны остаться в том же модуле.

**Сокращение.** Помощник `_outcome(ref, action, status="ok", head=…, **extra)` заменяет 33 литерала `"step": "observer-reconcile"` и тройной `observer-cursor-unavailable` (798–808, 845–855, 903–913). Это ≈−150 строк (**О**).

**Контекст.** Для задачи «доставка wake»: 40 тыс. токенов сейчас → ≈25 тыс. после разреза → ≈17 тыс. после сжатия прозы (**О**).

### 5.5 `runtime/local_pty_head.py` (3 445 строк, 180 тыс., проза 52 %) — DEC-05

Это не наследник `head/local_pty/*`, а другой слой: backend над substrate (**Ф**). Предложение — пакет `runtime/local_pty_head/`, в `__init__` реэкспорт текущих имён.

| Модуль | Содержимое | Символов |
|---|---|---|
| `runtime.py` | `LocalPtyHeadRuntime`, `AttachedStream` | 107 тыс. → ≈60 тыс. после сжатия прозы |
| `delivery.py` | Словарь исходов и отказов (243–377), `DeliveryReport`, классификаторы отказов | ≈25 тыс. |
| `durable.py` | `_DurableHead`, `_journal_state`, `_supervisor_state`, `_Probe`, `_Address` | ≈8 тыс. |
| `journal_view.py` | `_JournalReplay`, `head_run_turn_reading`, `head_run_screen_lines`, `head_run_journal*`, `head_run_loss_reason` | ≈20 тыс. |
| `inspect.py` | `SupervisorLease`, `head_run_supervisor_lease`, `fence_cleanup_scopes`, `runtime_scope_inventory`, `head_scope_owner_*` | ≈14 тыс. |

**Ограничение.** `tests/test_local_pty_head_runtime.py:2187` содержит whitelist одного файла backend; его нужно расширить до каталога пакета.

**Контекст.**

| Задача | До | После |
|---|---|---|
| Доставка | ≈45 тыс. токенов | ≈21 тыс. |
| Ридеры vitality и журнала | ≈45 тыс. | ≈6 тыс. |

**Сопутствующее сокращение:**
- DUP-R2: разбор `/proc/<pid>/stat` + `boot_id` встречается 6 раз, нужен лист `head/procfs.py`;
- DUP-R5/R6: дубли `HeadRun` и `AttachReceipt`;
- мёртвые `attach`/`request_drain` (≈300 строк). Это продуктовое решение, см. §6.1.

### 5.6 `webproto/sprint_reads.py` (2 773 строки, 135 тыс.) — DEC-06

| Модуль | Содержимое | Символов |
|---|---|---|
| `sprint_vocab.py` | 100–372 | ≈20 тыс. |
| `sprint_sections.py` | `SprintSections`, 395–1209 | ≈40 тыс. |
| `sprint_reads.py` | `SprintReadLayer`, 1217–1931 | ≈36 тыс. |
| `sprint_waits.py` | 2117–2324 | ≈12 тыс. |
| `sprint_derive.py` | 2341–2718 | ≈20 тыс. |

**Ограничения:**
- тесты патчат `sprint_reads.observer_snapshot`, `board_client`, `task_audit_for`, `_SOURCE_FAILURES`, поэтому вызывающие должны остаться в `sprint_reads`;
- `tests/test_architecture.py:868` содержит allow-list `task_audit_for(` по пути файла.

**Общая база слоёв** `webproto/layer.py: InstallationLayer(ProtocolBoundary)` с методами `__init__`, `report`, `data_dir`, `_instance_dir`, `_instance_file`, `_client`.
- Сейчас эти методы скопированы: `data_dir()` — 7 раз, `report()` — 6, `__init__` — 4.
- Выражение `self.instance.parent if self.instance.is_file() else self.instance` встречается 10 раз.
- Сокращение ≈−150 строк / −6 тыс.
- Риск: `ProtocolBoundary.__init_subclass__` оборачивает публичные методы; тест должен подтвердить, что обёртка сохраняется.

### 5.7 `web/pages.py` (4 996 строк, 250 тыс.) и `web/app.py` — DEC-07

**Состав `pages.py`:**

| Часть | Символов | Доля |
|---|---|---|
| CSS | 35,6 тыс. | 14,2 % |
| JS (8 констант + inline) | 22,7 тыс. | 9,1 % |
| HTML-литералы | 27,6 тыс. | 11 % |
| docstring | 28,3 тыс. | 11,3 % |
| Комментарии | 12,1 тыс. | 4,9 % |
| Python-логика | ≈124 тыс. | 50 % |

**Шаг 1.** Вынести `web/static/style.css` и `web/static/*.js`. Загружать их через `importlib.resources` в те же имена (`STYLE`, `_REFRESH_SCRIPT`, …): тесты используют `pages.STYLE` 27 раз и `_PO_SESSION_SCRIPT` 8 раз. Добавить `"ummanu.web" = ["static/*"]` в `pyproject.toml`. Два теста сканируют только `*.py` (`test_web_po_transport.py:627`, `test_web_status_bar.py:864`); их нужно расширить на `.css` и `.js`, иначе покрытие тихо пропадёт. Результат: 250 тыс. → ≈190 тыс. (−15 тыс. токенов на любую правку Python).

**Шаг 2.** Пакет `web/pages/` с реэкспортом (тесты используют ≈40 приватных имён):

| Модуль | Строки | Символов |
|---|---|---|
| `shell.py` | 518–999 | 22 тыс. |
| `parts.py` | 1000–1189 | 9 тыс. |
| `dashboard.py` | 1190–1653 | 22 тыс. |
| `history.py` | 1654–2118 | 21 тыс. |
| `card.py` | 2119–3240 | 52 тыс. |
| `sprint.py` | 3519–4238 | 33 тыс. |
| `po.py` | 4239–4996 | 36 тыс. |

Типовая задача на одну страницу: 70–90 тыс. символов вместо 250 тыс. (−40…45 тыс. токенов).

**`app.py`** (68 тыс.):
- `web/routes.py`: `Route`, `ROUTES`, наборы полей, ≈9 тыс.;
- `web/forms.py`: 1039–1270, ≈10 тыс.;
- в `app.py` остаются обработчики; помощник `_po_refused()` для трёх почти одинаковых блоков обработки отказа PO в `_po_send`/`_po_close`/`_po_rename` (`app.py:940-1013`), −25 строк.

**Сокращение.** Мёртвый код ≈8,5 тыс. (§6.1, DEAD-23). Дубли DUP-W1…W5 ≈20 тыс.

### 5.8 Сжатие прозы как отдельная «декомпозиция» — ARCH-01

**Это самый крупный рычаг.** Рекомендуемое правило:
- docstring модуля и функции — инвариант и контракт в 1–3 строки плюс ссылка на раздел docs;
- история инцидентов, номера карточек, «раньше» — удалить: они есть в git log и PR.

**Оценки по зонам** (**О**):

| Зона | Сокращение | Примечание |
|---|---|---|
| dispatch A | −130 тыс. | Сохранить первый абзац каждого docstring: −113 тыс. |
| vitality-кластер и `dispatch/gate.py` | −40…60 тыс. | |
| `runtime`/`po`/`automations` | −100…150 тыс. | |
| `web`/`webproto` | ≈−120 тыс. | |
| Ядро задач | −40…60 тыс. | |
| Установка | −20…30 тыс. | |
| **Итого `src`** | **≈−450…650 тыс. символов** | ≈−110…160 тыс. токенов |

**Предусловие для vitality-кластера.** Сначала исправить расхождения кода и `HEAD_VITALITY.md` (§8, DOC-09), потому что документ станет единственным источником.

### 5.9 Тесты — TEST-01

**`test_dispatcher.py`.** Разделить по классам и предметам:

| Новый файл | Объём | Примечание |
|---|---|---|
| `test_dispatcher_gate.py` | ≈32 тыс. токенов | Хосты gate, `ReviewBaseReconciliationTests`, `DispatcherGateTests`. Без Postgres-гейта уходит в `component`: 86 тестов выходят из Docker-шарда |
| `test_dispatcher_records.py` | ≈10 тыс. токенов | 11 небольших классов состояния, unit |
| `test_dispatcher_launcher.py` | — | |
| `test_dispatcher_head_prompt.py` | — | |

`DispatcherRuntimeTests` (328 тестов) делится по префиксам имён:

| Файл | Тестов |
|---|---|
| `review_rounds` | 116 |
| `claim_admission` | 60 |
| `worker_report` | 46 |
| `wait_vitality` | 39 |
| `runtime_gate` | 28 |
| `runtime_misc` | 39 |

Помощники класса переходят в `dispatcher_fixtures.py`.

**Остальные крупные файлы:**
- `test_dispatcher_observer.py`: 5 классов `RealHost*`/Codex-trust → `test_observer_real_host.py`.
- `test_dispatcher_launch_intent.py`: `HostLaunchContourTests` без Postgres → отдельный файл.
- `test_tasks.py`: `BoardFixture` (97–429) → `tests/fakes/tasks.py`.

**Результат.** Крупнейший файл 181 тыс. → ≈36 тыс. токенов. Чистое сокращение даёт только §6.4. Каждый новый файл нужно ровно один раз внести в `ci-shards.txt`, валидатор манифеста это проверяет.

### 5.10 Прочие кандидаты на разбиение (кратко)

| Файл | Предложение | Ограничения и выигрыш |
|---|---|---|
| `upgrade.py` (129 тыс.) | `installation/upgrade/{git,receipts,services,dependencies,workspaces,steps}.py`; `STEPS`/`run_steps`/`run_upgrade` в `__init__` | 20 `patch.object(upgrade, …)` и 17 строковых патчей перенацелить. Плюс DUP-I1: три реализации process-receipt, ≈−220…280 строк |
| `checkpoint.py` (120 тыс.) | `backup/checkpoint/{analytics,writer,exporter,pusher,status,audit}.py` | Всего 7 патч-целей, самый дешёвый разрез |
| `installation.py` (105 тыс.) | `installation/{clone,snapshot,secrets_step,materialize,projects,recover}.py`; туда же `bootstrap.py` (он импортирует 7 приватных имён из `installation`) | — |
| `restore.py` (73 тыс.) | Импорт доски (≈1 070 строк) → `backup/board_restore/` рядом с `task_restore.py` | 22 локальных импорта, часть уже есть в шапке |
| `dispatch/gate.py` (65 тыс.) | `github_ci.py` (≈450 строк: `_backend_call`, `_gh_api`, `_rollup`, `_check_*`, `_failed_log`, `rerun_failed_ci`…), `gate_pr.py` (863–1166), `gate_workflows.py` (676–797) | Устраняет ARCH-08; для задач e2e/post-merge 65 тыс. → ≈20 тыс. |
| `dispatch/cleanup.py` (79 тыс.) | `cleanup_journal.py` (1–370), `cleanup_inventory.py` (1063–1421) | — |
| `dispatch/review.py` | `command_terminal_status` и помощники (220–437, ≈10 тыс.) → `head_probe.py` | `head_status`/`status.py` перестают тянуть `review.py` |
| `head_vitality_episode.reduce_vitality` (501 строка) | `_quiet_ladder` (сливает 808–828 и 917–939), `_dark_diagnostic` (891–903 ≡ 1001–1008), `_record_availability` (620–664), `_child_hold` (691–711) | −60…90 строк, риск M; нужны характеризационные тесты на каждый вердикт |
| `sprints.py` (174 тыс.) | Close (34,7 тыс.) → mixin; guard index и admission (16,8 тыс.) → модуль; budget/resume (16 тыс.) → mixin | Не плодить третий `sprint_close*` |
| `cli.py` (90 тыс.) | doctor (≈47 тыс.) → `doctor_commands.py` по образцу `*_commands.py`; регистрация подкоманд лениво по группам (ARCH-12) | — |
| `po/service.py`, `po/runner.py` | `po/sprint_session.py` (590–814), `po/server.py` (1084–1203), `po/cli_output.py` (159–250), `po/scoped_launch.py` | — |
| `runtime/codex_preflight.py` (976 строк) | CODEX_HOME → `codex_home.py`; trust/config → `codex_trust.py`; fanout/provider events → `codex_fanout.py` | — |
| `head/local_pty/supervisor.py` | Сокетная часть (885–1226, ≈15 тыс.) → `supervisor_socket.py` (mixin) | — |
| `dispatch/state.py` | 8 обёрток `Persisted*` → база `_PersistedMapping` (−150 строк). Сериализация `DispatcherRecord`/`ObserverRecord` по таблице полей (−250 строк) | Риск M: порядок ключей, особые умолчания (`report_generation`, `claimed_at`); нужны golden-тесты round-trip |

## 6. Мёртвый код и доказанное дублирование

Все кандидаты из `vulture` перепроверены `grep`-ом по `src/`, `tests/`, `scripts/`, `packaging/`, `skills/` и `docs/`, включая строковые и динамические ссылки.

Ниже то, что `vulture` пометил, но что **на самом деле живо**:
- `_rpc_*` вызываются через `getattr(self, f"_rpc_{method}")` (`sql_cards.py:643`);
- `WebApp._*` — через `getattr(self, f"_{route.handler}")` (`app.py:375`);
- `do_GET`/`do_POST` — хуки `http.server`;
- `@mcp.tool` (`memory_search`/`get`/`list`) и `verify_token`;
- поля TypedDict (`pause.py:46,67`);
- `downgrade`/`down_revision` в миграциях;
- `legacy_codec.py` — у него 4 живых импортёра.

### 6.1 Мёртвый код, подтверждённый `grep`

| ID | Что | Где | Доказательство | Сокращение | Риск |
|---|---|---|---|---|---|
| DEAD-01 | `GATE_NAME_FOR_TASK_CLASS` | `dispatch/launch.py:627` | есть только определение | 1 строка | — |
| DEAD-02 | `_run_snapshot()` | `dispatch/state.py:1302-1303` | только определение | 2 | — |
| DEAD-03 | `_PYTHONPATH_PREFIX`, `_CONTROL_PLANE_TASK_COMMAND` | `dispatch/runtime.py:238-239` | не читаются, но держат импорты `:226,231` | 2 + импорты | — |
| DEAD-04 | 31 compatibility re-export | `dispatch/runtime.py`, `host.py` | у них нет потребителей (AST-скан) | ≈45 строк | низкий |
| DEAD-05 | свойство `CommandHostRuntime.head_runtime` | `host.py:3450-3461` | всегда бросает `LegacyDispatcherRecord`; используется только в тесте, который проверяет именно это | 12 + тест | низкий |
| DEAD-06 | состояние `"idle-recovering"` | `observer.py:880` | проверяется, но нигде не присваивается | 1 ветка | низкий |
| DEAD-07 | `ATTEMPT_USAGE_KIND`; `DispatchedRun.html_url` | `attempt_usage.py:63`; `e2e.py:224` | не читаются: вызывающие (`e2e_stage.py:309-331`, `e2e_after_merge.py:693-713`) строят `run_url` сами | ≈5 | — |
| DEAD-08 | ветки `inventory(catch_up=True)` и параметр `_residue_row(catch_up)` | `cleanup.py:1122,1125,1134,1142,1184-1188,1204-1205` | никто не передаёт `True`; единственная явная передача — `catch_up=False` (`:1301`) | ≈20 + docstring | низкий |
| DEAD-09 | pane-advisory путь в vitality | `from_pane_readiness` (`head_vitality.py:606-641`), обработка `"idle"` (`:809`), advisory-блоки редуктора (episode 666–689, 963–992), `"idle"` в `wait_vitality.py:1047` и `gate_lifecycle.py:863` | ни один редуцируемый производитель статуса не выдаёт `"idle"`; единственный источник — `host.observer_status` (`host.py:1591`), но он не попадает в `snapshots_from_status`. Поля `last_turn`/`turn_ended_at` в `from_json` оставить читаемыми | ≈110 + тесты | средний |
| DEAD-10 | 13 неиспользуемых параметров | `wait_vitality._recovery_thresholds(runtime)`:495, `_recovery_policy_decision(kind)`:511, `_sigcont_head(now)`:741, `_escalate_recovery_to_operator(now)`:833, `_vitality_guard_decision(runtime)`:875, `_guard_or_wait(records,payload,now)`:901, `_trigger_wait_watchdog(stall=)`:1182, `release_parked(reason)`:222, `reslice_parked(reason)`:216, `gate._local_gate(record)`:393, `e2e_stage._block(attempt_id)`:708, `_attempt_outcome_obligation(disposition)`:75, `post_merge.open_watch(runtime)`:103 | ruff ARG, проверено вручную | ≈30 | низкий |
| DEAD-11 | `head_vitality.SnapshotSource.EXECUTION_RECEIPT`, `RecoveryIntent.REQUEST_DRAIN/RESPAWN/BLOCK`; `ProgressState.STAGNANT` (только в одном `assertNotEqual`) | `head_vitality.py:138`, `head_vitality_policy.py:90-96` | только определения; в docstring названы «словарём» | ≈6 | решение владельца |
| DEAD-12 | `AcceptedGreenGate.persisted_payload` (используется только в тестах); `.valid` — чистый алиас | `gate_receipt.py` | — | ≈10 | низкий |
| DEAD-13 | `resource_probe_readiness`, `_recorded_readiness`, `print_resource_probes` и импорты `PROBE_TTL_SECONDS`, `HeadHealth`, `run_probe` | `cli.py:1348-1430`, `:45-48` | ссылок нет; doctor строит пробы через `collect_recovery_inventory` (`cli.py:895`). Нужно поправить `tests/test_cli.py:274`, который патчит `run_probe` | ≈70 строк / 3,2 тыс. | низкий |
| DEAD-14 | `_closeout_result`, `_source_audit`, `_resume`; импорт `SprintSourceAudit` | `sprints.py:3669,3809,3815-3824`, `:52` | ноль ссылок; `_resume` дублирует логику `:2474-2480` | ≈1 тыс. | — |
| DEAD-15 | 4 алиаса «released private compatibility»; реэкспорт `BUDGET_UNCHARGED_*` | `tasks.py:44-61`, `restore.py:37`; `sprints.py:55-60` | существование алиасов проверяет только `tests/test_tasks.py:5755-5761`; `BUDGET_*` используют только тесты | ≈0,7 тыс. | низкий |
| DEAD-16 | API, используемое только тестами | `broad_check.parse_unittest_summary`, `result_refusal`; `product_issues._transaction_event`; `onboarding.compatibility_manifests`; `SprintReader.status`; `DispatcherRuntime.resume_pipeline` (`runtime.py:429`); `watchdog.idle_stall_seconds`; `GitWorkspaceManager.teardown` (всегда бросает); `upgrade.running_product_root`; `host.SHIPPED_PACKAGING_ROOT`; `state_repo.MEMORY_PATHSPEC` | — | ≈100 | перенести в test-support или оставить |
| DEAD-17 | `memory/client_config.TOKEN_ENV` | `:24` | 0 ссылок; дублирует `runtime/role_env.py:127` | 1 | — |
| DEAD-18 | `events.marker_comment_lock` (файловый lock) с импортами `fcntl`/`hashlib`/`Path` | `board/events.py:50-65` | все вызовы идут в SQL-версию `.audit.marker_comment_lock` | ≈19 | — |
| DEAD-19 | `SqlAuditError`; `SqlTaskAudit.refusals()`; `ProductIssueRecords.key_of/row/metadata/comments`; `SqlSprintRecords.comments`; `SqlBoardHost._sprint_data` | `sql_audit.py:93,576`; `sql_product_issues.py:98,214,324,583`; `sql_sprints.py:639`; `sql_host.py:767` | ноль ссылок, кроме собственных копий в фикстурах | ≈35 | — |
| DEAD-20 | `BoardHost` Protocol | `board/host.py:193-206` | нигде не используется как тип; `FakeBoardHost` ему не удовлетворяет | 14 | низкий |
| DEAD-21 | Orca-эпоха в `runtime` | `AgentState.next_terminal_generation`/`load_terminal_*`/`terminal_generation_file` (`state.py:137,189-241`); `load_terminal_handle`/`save_terminal_handle` (`:180-187,243-276`, только тесты); `head_profile.json` (пишется в `dispatch.py:887`, не читается); модуль `automations/agents/pipeline/codex_sessions.py` (125 строк); `pipeline/naming.SLUG_RE`, `pause.MODES`/`PUBLIC_MODES`; бо́льшая часть `agent_prompt_transport` (≈85 из 107 строк); `OBSERVE_INVENTORY_UNREADABLE`/`OBSERVE_PANE_ABSENT`; `HeadActivity.acted/observed/_output_marks/busy`, `HeadReceipt.left_alive/unsupported`; тестовый API клиента (`wait_for_delivery`, `next_event`, `stream`, `resize`, `HeadHandle.connect/events/identity`, `journal.events_since`); `heads.profile_info`; `PoStore.begin_turn`; `PoService.exiting` | разные | `grep` | ≈22 тыс. символов | низкий |
| DEAD-22 | Неиспользуемые продуктовые глаголы `LocalPtyHeadRuntime.attach` (1382–1472) с `AttachedStream` и `request_drain` (1243–1285); push-attach в supervisor (`:1091-1114,1167-1194`) | — | нет ни одного вызова в `src` | ≈300 строк / 14 тыс. | **продуктовое решение**: глаголы входят в Protocol, на них ≈19 тестов |
| DEAD-23 | Web | `_DASHBOARD_SCRIPT` (`pages.py:3246-3288`), `_start_form` (2226–2250), `_review_form` (2963–2975) и review-блок `_TASK_SCRIPT` (3487–3516), `_feed` (1657–1661) и ветка `compact`, `_project_table`/`_task_table` (2195–2223), CSS `.sprint-card`/`.stack`/`#feedback`, `FORM_TYPE`, `FEED_LIMIT`, `DEFAULT_SITES`, `RUN_ROLES`, `ProductRun.at_phase`, `CLOSE_STATES`, `_BOARD_SETTLED_STATES` | — | ноль ссылок; составные классы CSS проверены | ≈8,5 тыс. | — |
| DEAD-24 | Скрипты | `scripts/ummanu-start.sh` («Pinned Orca entry point», есть только в `test_pipeline_paths.py:65`); `check_memory_mcp_restore_e2e.py` (нигде не запускается, не исполняемый); `repro_local_pty_retained_continuation.py` (разовый repro, зависит от необъявленного `pyte`); `scripts/role_skills.py` (8-строчный shim, используется subprocess-ом из `steward/cli.py:41,170`) | — | — | ≈18 тыс. символов | низкий |
| DEAD-25 | `backup create --no-copy-transcripts` | `cli.py:366-370` | принимается и не читается. **Уточнение оркестратора ✔**: флаг скрыт (`argparse.SUPPRESS`), то есть это намеренный no-op для совместимости, а не забытый флаг | 5 строк | решение владельца |

### 6.2 Одноразовый миграционный код (вывод из эксплуатации — решение владельца)

**DEAD-T. Переход secretary → ummanu завершён.** Основания (**Ф**):
- `RENAME.md` §T6 закрыт до карточки #8 («Live proof `ummanu-1`»);
- переименование в коммите `ad286ae`, послепереходные исправления в `85ef095` и `b6e0d0e`;
- текущая работа идёт карточками `ummanu-86`.

Что можно вывести из эксплуатации:

| Что | Объём |
|---|---|
| `src/ummanu/transition/` (13 файлов) | 3 282 строки / 143,7 тыс. |
| `scripts/rename_to_ummanu.py` (по собственному docstring — «may be deleted»), `transition-from-secretary.sh` | 15,4 тыс. |
| Тесты `test_transition_{from_secretary,board,products}.py` | 82,8 тыс. |
| `RENAME.md` | ≈56 тыс. |

**Связи, которые надо разорвать заранее:**
- `cli.py:114,253` (команда `transition`);
- `automations/agents/curator/rebind.py:30` (`rewrite.claude_move_paths/claude_key/swap_prefix`);
- `infra/old_name_guard.py:18` и `infra/live_root_findings.py:23` (`names.INSTANCE_PROJECT/NEW`);
- ссылки на §T1/§T3 из `entrypoint_guard.py:26`, `production_checkout.py:15`, `upgrade.py:282`.

**Что оставить:** `old_name_guard` и `live_root_findings` — это постоянные guard-проверки doctor.

**Риск — M.** Пропадут rollback и `--repair-*`; второй, ещё не мигрированный хост (если он есть) потеряет путь перехода.

**DEAD-R.** `board/reference_repair.py` (18 тыс. символов) привязан к `PRODUCER_FIX = "d9e872ba…"` для ошибки уже выпущенного аллокатора. Ни один тест его не импортирует. Вывести можно только после того, как на живых хранилищах подтверждено отсутствие дублей и pending-строк `reference_repaired` (**Г**).

**DEAD-H1 (Г, высокий выигрыш).** На PostgreSQL pending/recovery-машинерия `SqlBoardHost.recover_*` (`sql_host.py:464-660`, ≈7 тыс.) и связанные части `events.MutationEventTransaction` и `tasks.reconcile` могут быть недостижимы. Все вызовы мутаций идут внутри `client.transaction()`, и `tasks.py:5141-5142` сам пишет, что `recover_*` «there have nothing to do». Проверка: доказать, что в production протокольные строки `requests` никогда не остаются в статусе staged.

### 6.3 Доказанное дублирование

Только пары с точными ссылками на обе стороны. Рядом с каждой — почему извлечение безопасно.

**Dispatch**

| ID | Сторона A | Сторона B | Безопасность и комментарий | Сокращение |
|---|---|---|---|---|
| DUP-D1 | `observer.py:1661-1673` | `worker_continuation.py:935-947` | Классификация «residual composer» — чистая функция от evidence. Вынести `residual_composer_class(evidence)` в `worker_lifecycle` | 12 |
| DUP-D2 | `production.py:195-237` `_record_incident` | `runtime_preflight.py:419-440` | Копия в `runtime_preflight` **намеренная**: модуль обязан быть stdlib-only (docstring 1–11). Безопасно в обратную сторону — `production` импортирует помощник из `runtime_preflight` | 25 |
| DUP-D3 | 8 классов `Persisted*` (`state.py:136-651`) | — | Одинаковые `__init__`/`from_value`/`to_json`. Нужна база `_PersistedMapping` с хуком `_parse`, `__slots__` и имена свойств сохраняются. Вдобавок явные `from_value` в `from_json` (`:1184-1256`) избыточны: `__setattr__` (`:1022-1042`) и так нормализует | 150 |
| DUP-D4 | `stop_review_head` 2439–2460 | `stop_worker_head` 2509–2530 | Сюда же: `review_lifecycle_run`/`worker_lifecycle_run` (2478–2507 / 2554–2583), 6 копий `if self.commit_state is not None: self.commit_state()`, блок привязки identity в `provider_progress`/`provider_failure` (1763–1769 / 1796–1802). Таблица полей по ролям; она же снимает CON-06 | 100 |
| DUP-D5 | `_head_status(...)` ×5 (`host.py:3556,3576,3632,3663,3710`) | `_record_heartbeat_status` (`:3381-3390`) | Тот же расчёт. Четыре вызова с одинаковыми 6 kwargs в 2633–2765 сводятся к `functools.partial` | 50 |
| DUP-D6 | `host.py:3134-3147` | `host.py:3162-3175` | Preflight до и после ветки `noop` идентичен, `heartbeat_identity` — чистая функция | 14 |
| DUP-D7 | `observer.py` 798–808, 845–855, 903–913; 939–947 / 981–988 | — | Одинаковые outcome-dict, плюс 33 литерала `"step": "observer-reconcile"` | 150 |
| DUP-D8 | `worker_lifecycle.py:479-501` | `:532-554` | Два одинаковых вызова `cls(...)` с 24 аргументами; различается только `source_rejected`. Условие «все поля baseline пусты» записано 3 раза. Порядок ранних возвратов сохранить | 35 |
| DUP-D9 | `worker_report.py` | — | Блок `_stop_worker_confirmed → return → terminal_effect(blocked…)` повторён 8 раз; сброс gate «fresh code state» — `177-184` ≡ `353-360` | 50 |
| DUP-D10 | `production.py:457-492` | — | 6 одинаковых `try/except → _unexpected_error`; `skipped.append({...})` 10 раз в `_production_claim_ready` (1609–1736) | 45 |
| DUP-D11 | `e2e_stage.py:336-477` `_identify`/`_create_wait` | `e2e_after_merge.py:814-946` | Сходство по diff 0,70/0,76. Различаются 4 точки: `run.branch`/`git_ref`, текст, persist-функция, префикс request id (`e2e-recovered` против `e2e-am-recovered`). Параметризовать, байты сообщений и request id сохранить. Обработка dispatch-result (`e2e_stage.py:308-331` ≡ `e2e_after_merge.py:692-713`) — туда же | ≈120 |
| DUP-D12 | `e2e.py:260-263` | `e2e.py:328-332` | `dispatch_workflow` переписывает `_gh_status` | 4 |
| DUP-D13 | `gate.py:799-809` `_actions_run_id` | `gate.py:1323-1331` | Плюс `(x.stderr or x.stdout or '').strip()` 15 раз и чтение HEAD sha (`:596` и `:1208-1216`) | ≈25 |
| DUP-D14 | `wait_vitality.py:1059-1081` | `head_status.py:216-231` | Извлечение предыдущего курсора. Общий `snapshots_for_episode(...)` заодно чинит BUG-15 | ≈20 |
| DUP-D15 | `wait_vitality.py:1044-1050` (5 ключей) | `gate_lifecycle.py:860-864` (3 ключа) | Предикат «ничего не наблюдалось» **уже разошёлся**. Копию в gate удалить: `reduce_and_store_vitality_episode` повторяет проверку | 5 |
| DUP-D16 | `_PROGRESS_SOURCE_NAMES` (`head_vitality_guard.py:61`) | `_PROGRESS_SOURCES` (`head_vitality_episode.py:102`) | Оба `{"provider_cursor"}`. Сюда же `host.DESTRUCTIVE_VERDICTS` (`host.py:331`): нужен только `wait_vitality`, место — рядом с `VitalityVerdict` | 5 |
| DUP-D17 | `read_merge_gate` (`review_verdict.py:314-416`) | `read_release_gate` (`release_lifecycle.py:296-389`) | Общие только ветка transport и блок `result is None → *-gate-result-blocked`, внутри `review_verdict` он повторён 3 раза. Обработка drift/red **намеренно различается** | 40 |
| DUP-D18 | `git worktree list --porcelain` разбирается 5 раз | `cleanup.py:94-111`, `infra/git_worktree.py:46-50`, `host.py:1249,2854`, `upgrade.py:1215` | Один парсер в `infra.git_worktree` | ≈30 |
| DUP-D19 | `wait_vitality.py` | — | 14 outcome-dict собираются вручную; `"step": "review" if kind == "review" else "advance"` — 10 раз; `record.review_X if kind == "review" else record.worker_X` — 30 раз (вместе с `review`/`head_status`). Помощник `_wait_outcome` и view `record.role(kind)` | ≈80 |

**Ядро, CLI, установка**

| ID | Сторона A | Сторона B | Комментарий | Сокращение |
|---|---|---|---|---|
| DUP-C1 | `check_commands.py:140-145` `_missing` | `product_issue_commands.py:120-125` | Байт в байт. Тот же usage-обработчик в `sprint_commands.py:298`, `task_commands.py:415`, `webproto/commands.py:128,334`, `dispatch/commands.py:143`; печать ошибки `json.dumps({"error": …})` — 8 раз. Вынести `cli_output.usage_handler` и `print_error` | ≈60 |
| DUP-C2 | `_fsutil.py:203-211` `file_lock` | `sprints.py:247-255`, тело `tasks.py:690-701`, inline `product_issues.py:199-204,238-264` | Одинаковый flock-шаблон | ≈30 |
| DUP-C3 | `"instance.yaml" if p.is_dir() else p` — 13 раз | например, `cli.py:2233`, `task_commands.py:75`, `sprints.py:316`, `backup.py:507`, `config.py:232`, `restore.py:1501` | Плюс ≈15 помощников `_instance_dir`/`_instance_file`. Добавить `runtime/paths.instance_file()` | ≈40 |
| DUP-C4 | `tasks.py:1861-1901` (метаданные create) | `tasks.py:5877-5920` (`_create_metadata_values`, repair) | **Уже разошлись** (BUG-12) | 1,9 тыс. |
| DUP-C5 | `tasks.py:5376-5382` | `tasks.py:5428-5434` | Один и тот же предикат «Ready cleanup incomplete» | 7 |
| DUP-C6 | `move`: replay `tasks.py:3747-3775` | fresh `3811-3839`; `_guard_sprint_write` `3776-3787` ≡ `3906-3917` | — | 1,2 тыс. |
| DUP-C7 | `"backend write committed; audit repair is required"` | 14 раз в `tasks.py` | Константа или фабрика | 1 тыс. |
| DUP-C8 | `sprints.py:915-946` `_sql_atomic` | inline `sprints.py:1087-1101` | Магическое число advisory lock `1_600` дважды | 15 |
| DUP-I1 | `upgrade.py` process-receipt: память `1553-1679` | web `1786-2016`, PO `2064-2174` | У каждого свои `*_INPUT_KEYS`, `_valid_*`, `_read_*_receipt`, `_*_evidence`, `_*_summary`. Web-ридер (`1936`) переписывает `_load_receipt` (`446`) **без** size cap и `O_NOFOLLOW` и принимает `version: true`. Шаги `step_memory`/`step_web` повторяют последовательность restart → identity → probe. Решение: спецификация `ProcessReceipt` и `_restart_probe_bind()`. JSON на диске должен остаться байт-совместимым | 220–280 |
| DUP-I2 | «Последняя строка stderr git» — 14 раз | `installation` ×6, `checkpoint` ×4 (`:834`, `:1842`…), `snapshot_tree:247`, `state_repo:336,371` | `_git`-обёртки (`checkpoint.py:827,1808,2359`, `snapshot_tree.py:241`, `upgrade.py:222`) различаются только классом исключения | ≈40 |
| DUP-I3 | `checkpoint._SnapshotAudit._blobs:2323` | `infra/snapshot_tree._blobs:253` | Версия в checkpoint менее устойчива (INEF-08) | 20 |
| DUP-I4 | Атомарные писатели в обход `_fsutil` | `upgrade._write_private_receipt:2019` (богаче: chown + fsync каталога), `memory/access._write_json:325`, `secret_store._write_key_file:336` | Расширить `_fsutil` параметрами mode/owner/fsync | ≈40 |
| DUP-I5 | `restore._entity_number:103` ≡ `task_restore:42`; `_set_restore_phase` (`restore:270` ≡ `task_restore:50`); `_unsafe_member` (`restore:1599` ≡ `backup_verify:283`); `backup_policy._relative_to_data:262` ≡ `_fsutil.display_relative:300` | — | Литерал модели памяти по умолчанию — в 5 местах (`memory/__init__:6`, `host.py:108`, `restore.py:728`, `restore_commands.py:213`, `host_apply.py:404`) | ≈40 |

**Board**

| ID | Где | Комментарий | Сокращение |
|---|---|---|---|
| DUP-B1 | `_now`/`_epoch`/`_rfc3339`/`_grouped`: `sql_cards.py:218-277` ≡ `sql_sprints.py:27-42` | `sql_product_issues.py:635-654` обходит их ленивыми импортами. **Внимание:** `sql_sprints._rfc3339` сохраняет микросекунды, `sql_cards._rfc3339` их отбрасывает (INEF-10); при объединении оба поведения сохранить явно | ≈45 и 8 локальных импортов |
| DUP-B2 | Хеш event-id ×5: `sql_host.py:836-859,955-969,1188-1202,1255-1271`, `fake.py:318-336` | Чистая функция `event_id(identity)` | 25 |
| DUP-B3 | Board-row dict (10 ключей) ×6, comment dict ×3, разбор маркера ×3, декодирование extensions ×5, SQL слияния jsonb-bag ×3 | См. §5.3 | ≈70 |
| DUP-B4 | `_bag()` ×3 (`wait_card.py:355`, `e2e_budget.py:139`, `owner_handover.py:71`); `_json_field()` ×3 (`wait_card.py:361`, `e2e_record.py:411`, `po_origin.py:43`) | В `extension_bag.py` | 30 |
| DUP-B5 | `events.render_marker_comment` (`events.py:68-98`) повторяет `models._validate_control_marker_event` (`models.py:528-560`) | Проверки в render **недостижимы**: `Event` не создать без проверки в models (`:384`) | 15 |
| DUP-B6 | `models._validate_attempt_outcome_event` (`:692-793`, 5,8 тыс.) | Повторяет `AttemptOutcomePayload.from_data`/`__post_init__` (`attempt_outcome.py:66-79,186-318`). Константы `ATTEMPT_OUTCOME_*` — дважды. Внимание: меняются тексты ошибок, и `version=True` начинает отвергаться | 5 тыс. |

**Runtime, PO, automations**

| ID | Где | Комментарий | Сокращение |
|---|---|---|---|
| DUP-R1 | `automations/runtime/dispatch.py:463-488,491-516,519-557,560-587` | 4 функции одной формы. Сводятся к `_move_report(...)` | 70 |
| DUP-R2 | `/proc/<pid>/stat` + `boot_id` разбирается 6 раз: `identity.py:55-59`, `supervisor.py:1320-1354`, `scoped_lifecycle.py:65-73`, `po/runner.py:96-111`, `scope_inventory.py:150,174`, `children.py:52` | Седьмая копия (встроенный writer в `command.py:268-275`) **должна остаться inline**. Лист `head/procfs.py` на stdlib | 60 |
| DUP-R3 | `supervisor.py:326-335` `_socket_answers` ≡ `client.py:317-326` `_answers` | В `protocol` | 10 |
| DUP-R4 | fsync каталога ×5 (`client.py:190-194`, `supervisor.py:512-516`, `po/runner.py:383-387`, `scoped_lifecycle.py:212`, `po/queue.py:82`) | Помощник | 20 |
| DUP-R5 | `local_pty_head.py:795-796` ≡ `828-836` (дважды `HeadRun`); 3 `AttachReceipt` (1413–1456); `HEAD_GONE` (1205–1220 ≡ 2461–2473) | Помощник `_receipt_at` | 50 |
| DUP-R6 | Поиск `run.exited` ×4 (`local_pty_head.py:3133-3141,3370-3388`, `po/runner.py:282-287`, `ScopedHeadLifecycle.started_or_exited`) | Один журнальный помощник с нижней границей `run.started` (чинит BUG-16) | 25 |
| DUP-R7 | `tui_delivery.DeliveryEvidence.to_json` (179–226) перечисляет 48 полей вручную | `asdict`; сначала проверить, что хеширование не зависит от порядка | 45 |

**Web**

| ID | Где | Комментарий | Сокращение |
|---|---|---|---|
| DUP-W1 | Базовая обвязка слоёв (§5.6) | — | 150 |
| DUP-W2 | `command_reads.py:247-271` ≡ `pause_reads.py:170-199` (`_source`); `_installation()` (`command_reads.py:611-646` ≈ `pause_reads.py:545-590`) | — | 70 |
| DUP-W3 | `_reason` ×5, `_text` ×6 (webproto), `_float` ×2 | `webproto/_values.py` | 30 |
| DUP-W4 | `SprintRequestStore` (`sprint_requests.py:88-186`) и `RunStore` (`runs.py:345-430`) | Общий `RequestIndex`. Форматы на диске различаются (`reference` против `run_id`), поэтому классы записей оставить раздельными | 60 |
| DUP-W5 | `pages.py`: `_mapping` (`:2520`) ≡ `_block` (`:2608`); 33 повтора `x.get(k) if isinstance(...) else {}`; 5 context-manager-ов, различающихся только переменной (`:584-658`); 4 JS-fallback `crypto.randomUUID` | — | ≈2,5 тыс. |

**Намеренное дублирование, которое оставить:**
- `runtime_preflight` обязан быть stdlib-only;
- встроенный heartbeat-writer;
- `head_runtimes` против `head_runtime_backends` — разрыв цикла;
- миграции заморожены (`0006`, `0013` копируют формулы намеренно, есть тест-сверка);
- `transition/secret_rewrap` копирует криптографию `secret_store`, ему нужны литеральные старые константы;
- `data.normalize_sprint_entity` — байт-стабильный формат чекпоинта;
- таблицы `_CODES` по слоям `webproto` отображают разные исходные словари;
- `product_issues._named_swimlane` (точное совпадение) и `tasks._matching_swimlane` (нормализованное) — разная семантика.

**Нарушение заморозки миграций:** `0004_product_issue_sql.py:9` импортирует живой `record_key`. Если схема ключей изменится, хранилище, обновлённое через старые ревизии, получит другие ключи, чем свежее (**Ф**, риск L–M).

### 6.4 Тесты: мёртвое и дублированное

| ID | Что | Доказательство | Выигрыш |
|---|---|---|---|
| TDEAD-01 | `tests/fixtures/recovery-order-groups.json` (26 КБ) | ноль ссылок | −26 КБ |
| TDEAD-02 | `recovery-card-shape-1440.json` (247 КБ, 11 530 строк) | единственный вызов — `_production_task_cards(40)` (`test_bulk_card_restore.py:198`); первые 40 записей и есть 40 задач | −240 КБ (≈60 тыс. токенов, если агент его прочитает) |
| TDEAD-03 | Мёртвые помощники: `test_tasks.py` (`board_batches` 160, `board_writes` 170, `assertCardCarriesNoMetadata` 218, `archive_card` 308, `board_refuses_once` 355, `board_drops_the_call_after` 386, `board_moves_the_card_after` 405, `_pending_typed_move` 960); `test_sprints.py` (`_reject_removal` 244, `_stall_create` 255, `_round_trips` 2442); `test_cli._raw_dump_instance` 735; `test_head_vitality_legacy_path.run_worker_to_validate_and_review` 156; `sql_backend_fixtures.close_card` 583 | ссылки только на определение (исключены `_*_refuses`, которые диспетчеризуются через `_faults`) | ≈200 строк |
| TDEAD-04 | Тесты-дубли: `test_head_health.py:87` ≡ `:345` (тела идентичны) | — | 1 тест |
| TDEAD-05 | `test_po_service.py`: `OutcomeUnknownTests(EndpointTests)` (858) и `AcceptanceAnswerTests(EndpointTests)` (971) наследуют 4 теста без переопределений | 8 лишних прогонов | время CI |
| TDEAD-06 | `test_tasks.py:5383` `TypedMarkerRecoveryTests(RequestIdOwnershipTests)` повторно гоняет 11 Postgres-тестов | **Г**: наследование выглядит случайным | время CI |
| TDUP-01 | 57 блоков `self.writer.report(role="worker", …, kind="done", …)` при существующем `_report_done` (используется 27 раз); 38 блоков `writer.verdict(...)` при `_review_red`; 72 повтора `TemporaryDirectory + _build_gated_workspace + GithubGateHost` (`test_dispatcher.py`) | — | ≈700 строк |
| TDUP-02 | setUp диспетчера ×8 (`dispatcher_fixtures.py:116-158`, `test_dispatcher_launch_intent.py:527-575` и `:4137-4172`, `test_dispatcher_observer.py` ×2, `test_dispatcher_review_lifecycle.py`, `test_head_vitality_legacy_path.py:43`, `test_live_telemetry.py`, `test_observer_metadata.py`); `start_dispatcher` ×2 | — | ≈250 строк |
| TDUP-03 | `git()` ×38 (готовый `tests/support/git.py:7` используют 4 файла); `_dead_pid` ×8; `_alive` ×7; `_kill` ×5; `"version: 1\nname: test\n"` ×20 | `tests/support/process.py`, `tests/support/instance.py` | ≈450 строк |
| TDUP-04 | `Recording` (`web_fakes.py:10` ≡ `test_web_transport_dashboard.py:34`); `FakeHeadRuntime` (`test_web_run_protocol.py:126` ≈ `test_web_transport.py:132`); `open_sprint` (`test_tasks.py:64` ≡ `fakes/tasks.py:22`); `drop_cards` (`test_sprints.py:60` ≡ `fakes/sprints.py:45`); `board_injected` (`webproto_sprint_fixtures.py:127-142` ≈ `fakes/sprints.py:299-315`); setUp `test_checkpoint.py:104` ≡ `test_knowledge_write.py:48`; `fast_key_params` ×2; `stop_everything` ×2 | — | ≈200 строк |
| TDUP-05 | Проза в тестах: docstring 762 тыс. + комментарии 338 тыс. (≈275 тыс. токенов); 3 357 ссылок на тикеты и даты; 126 docstring длиной ≥ 9 строк | Ограничить docstring теста формулировкой правила и одной ссылкой | −250…350 тыс. символов |
| TORG-01 | Помощники лежат в трёх местах: 28 модулей в корне `tests/`, `tests/fakes/` (7), `tests/support/` (3) | Правило: инфраструктура → `support/`, двойники → `fakes/`; в корне только `test_*.py`, `__init__.py`, `broad.py` | контекст |

## 7. Расхождения схем, API, событий и конфигурации

| ID | Контракт | Расхождение | Доказательство | Приор. | Метка |
|---|---|---|---|---|---|
| CON-01 | `instance.schema.json` и `examples/instance` | Блок `heads` проходит схему (`instance.schema.json:54`) и есть в примере. Но `host.build_plan:207-218` превращает каждую голову в юнит `{prefix}{role}.service`, а таких юнитов нет. Dry-run `apply_host` на примере выдаёт `no unit file is shipped for: ummanu-reviewer.service, ummanu-worker.service`; без `heads` — `[]`. Кроме того: `orca_repos` («legacy and ignored», 0 читателей), `persona.name/style` (0 читателей); ключи `host.memory_reindex_*`/`memory_model`/`dim` не документированы | воспроизведено субагентом | P2 | Ф |
| CON-02 | `web-run.schema.json` | `ops.run_list` выдаёт `kind: "product_runs"` (`ops.py:494`). `validate(doc, "web-run")` → `failed oneOf constraint`, хотя `PROTOCOLS.md:3775` утверждает, что каждый документ web-run валиден. Схем вообще нет у `head_view`, `owner_events*`, `po_*`, `health`, ответа codex reset, ответов `task_comment`/`task_move`. У `po_*` и `owner_event_read` нет `schema_version`/`observed_at`. Ни у одной схемы нет `additionalProperties: false` на верхнем уровне, поэтому дрейф не ловится | воспроизведено | P2 | Ф |
| CON-03 | Исход шага тика | `gate_lifecycle.py:833` (stall gate-pending) и `wait_vitality.py:1460` (эскалация watchdog) возвращают `{"status":"ok", "to":"blocked"}`, остальные пути блокировки — `"status":"blocked"`. Потребитель `production.py:1225` ставит fence на спринт и проект только при `status == "blocked"`, поэтому эти блокировки в том же цикле не огораживаются. Та же форма в `worker_report.py:239`, `worker_launch.py:818` | ✔ код прочитан | P2 | Ф (намеренность — Г) |
| CON-04 | Request id «своих блоков» в claim | `claim.py:626-651` ищет `_attempt_request_id(attempt_id, action, ref)` без суффикса. Производители `worker-wait-stall`/`review-wait-stall` (`wait_vitality.py:1435-1437`), `worker-respawn-blocked` (`:1331-1332`), `review-blocked` (`review.py:200-204,1029-1031`) добавляют суффикс цикла (`watchdog.py:281-290`). На главном пути attempt id новый (`production.py:1697`), так что ни один из 42 поисков не совпадает (INEF-03) | — | P2 | Ф |
| CON-05 | Словари доски | Issue kind/priority/close-reason: enum `models.py:125-143`, наборы в `product_issues.py:43-45`, литералы в `sql_host.py:217,934-935`. Маркеры: `models.py:545-559` ≡ `events.py:79-93`; набор `{CARD_REPORTED, CARD_VERDICTED, CARD_DECIDED}` — трижды. ≈66 литералов состояний карточек вне `board` | — | P3 | Ф |
| CON-06 | Роль ревьюера | `REVIEW_ROLE = "review"` (`launch.py:83`) против `"reviewer"` (HeadRun, heartbeat). ≥ 6 конвертеров: `host.py:4960`, `heartbeat.py:19`, `launch.py:533,615`, `host.py:1762,1795,2433`, `provider_failure.py:247`. `_launch` принимает оба (`host.py:3124,3327`) | — | P3 | Ф |
| CON-07 | Код ошибки «конфиг инстанса невалиден» | `sprint_reads`, `reads`, `ops`, `card_ops`, `sprint_ops` → `InstallationUnavailable` (503); `pause_reads:589`, `command_reads:645` → `ValidationRefused` (400), и docstring называет это «compatibility promise» | — | P3 | Ф |
| CON-08 | Формат времени | `record_e2e_intent`/`record_after_merge_intent` пишут `datetime.now(UTC).isoformat()` (микросекунды, `+00:00`; `tasks.py:2596,2694`), остальное — `_now()` (секунды, `Z`). Board: микросекунды у спринтов (`sql_sprints.py:35`) против усечения у карточек (`sql_cards.py:251`) против `strftime` в audit (`sql_audit.py:108,559`) | — | P3 | Ф (влияние на сравнение строк — Г) |
| CON-09 | `runtime.env` | `runtime_env.read_runtime_env` (`:23-74`) отвергает `export` и оставляет кавычки; `role_env.load_env_file` (`:279-313`) принимает `export` и снимает кавычки через shlex; третий парсер — `secret_store.py:969`. `KEY="a b"` читается по-разному | — | P2 | Ф (наличие таких значений — Г) |
| CON-10 | Вывод CLI | JSON задач — `ensure_ascii=False` (`cli_output.py:11`), спринтов — по умолчанию (`sprint_commands.py:320,336`). Голые группы `data`/`memory`/`config` печатают «not implemented in Phase 1 skeleton» и выходят с кодом 1 (`cli.py:120,2260`), а `task`/`sprint`/`product`/`check` дают JSON usage и код 2. `--data-dir` по умолчанию берёт `UMMANU_DATA_DIR` только у task/product. Коды ошибок `run_owner_decisions`/`run_resume` различаются (`sprint_commands.py:509-511,606-611`) | — | P3 | Ф |
| CON-11 | Чтение с записью | `SprintReader.show` → `ensure_sprint_board` может выполнить `createProject` (`sprints.py:638`); `list()` по умолчанию `create=True` (`:581`) | — | P3 | Ф |
| CON-12 | Fake против SQL | У `FakeBoardHost` нет `marker_comment`/`recover_*`; `transition` без role authority; нет автоматических связанных ref; close Issue не ставит `close_reason`; event-id без payload; `occurred_at` усечён до секунд; create/replace разрешены для любого kind; `MemoryAudit` бросает `TaskError`, fake host — `ValueError` | — | P3 | Ф |
| CON-13 | Протокол local-pty | `OP_RESIZE`, `OP_ATTACH`, `EVENT_OUTPUT/DROPPED/EXITED` не используются продуктом. `next_event` возвращает `None` и на тишину, и на закрытие, а docstring обещает их различать (`client.py:537-570`). `SUN_PATH_MAX = 100` против `po/service.MAX_SOCKET_PATH_BYTES = 107`. Комментарий `journal.py:30-36` («< 256 байт») ложен: `run.started` с heartbeat-обёрткой ≥ 2,1 КБ (измерено 2 175) | — | P3 | Ф |
| CON-14 | Prompt-транспорт | `agent_prompt_transport` декларирует «каждый prompt проверяется здесь», но local-pty доставляет `pointer.text + "\n"` без проверки (`local_pty_head.py:3023-3025`, `operations.py:118-121`) | — | P3 | Ф (риск — Г: входы доверенные) |
| CON-15 | Префиксы env | Смешаны `TA_*` и `UMMANU_*`: `TA_RUNTIME_ENV_FILE`/`UMMANU_RUNTIME_ENV_FILE` (`role_env.py:26-31`), `TA_CODEX_SESSIONS` против `UMMANU_CODEX_SESSIONS`, `TA_WORKSPACES_ROOT`, `TA_STEWARD_STALE_HOURS`. Литерал `"UMMANU_INSTANCE"` — 4 раза | — | P3 | Ф |
| CON-16 | `data-manifest.json` | Блок `components` валидируется, но не читается (`data.py:66`). В примере `facts: memory/facts`, а код пишет `state/memory/facts` | — | P3 | Ф |
| CON-17 | Blocked-reason gate | `merge_terminal_reason` классифицирует по подстроке `"gate"` (`release_lifecycle.py:44-48`). Нечитаемый merge-gate на пути review — `merge-gate-blocked` → `gate` (`review_verdict.py:360`), а на пути release — `release-failed-blocked` → `implementation` (`release_lifecycle.py:372`) | — | P3 | Ф (намеренность — Г) |
| CON-18 | Политика восстановления | Эскалация детерминированного отказа срабатывает в фазе gate (`gate_lifecycle.py:796`), но на тике HealthyQuiet/HealthyActive `_run_recovery_policy` (`wait_vitality.py:591-629`) сохраняет `rung=4` и молча теряет намерение ESCALATE. `RecoveryIntent.NUDGE` (rung 1) никогда не исполняется через политику | — | P2 | Ф (непреднамеренность — Г) |

## 8. Документация против реализации; возможности сжатия

### 8.1 Объём

| Документ | Символов | ≈Токенов | Комментарий |
|---|---|---|---|
| `PROTOCOLS.md` | 364 743 | 91 тыс. | ≈120 разделов; 5 разделов дописаны в конец вне структуры (4710–4964), с другой шириной переноса |
| `OPERATIONS.md` | 219 825 | 55 тыс. | «Manual curator routing» (7 тыс.) лежит под `## Upgrade` (`:3199`) |
| `BOARD_STORE.md` | 80 815 | 20 тыс. | 46 % абзацев называют ≥ 3 символов кода |
| `RECOVERY.md` | 72 699 | 18 тыс. | — |
| `RENAME.md` | 60 952 | 15 тыс. | ≈90 % — закрытая история перехода |
| `HEAD_VITALITY.md` | 38 968 | 10 тыс. | 53 % абзацев — пересказ кода |
| `ARCHITECTURE.md` | 38 213 | 10 тыс. | — |
| `HEAD_RUNTIME.md` | 36 950 | 9 тыс. | Таблица parity (6 тыс.) и чек-лист A20 (11,2 тыс.) — история |
| Прочие docs | ≈55 тыс. | — | `OWNED_CLEANUP` устарел, `REQUESTS_GROWTH` — запись решения |
| `skills/**/SKILL.md` | 108,7 тыс. | 27 тыс. | `observe-sprint` 33 тыс., `steward` 21,8 тыс. |

Дословное дублирование между docs < 1 % (шинглы по 10 слов: OPERATIONS↔PROTOCOLS — 107 совпадений). Единственный значимый дубль в prompt: раздел «Quoted standing owner decisions» (≈1,9 тыс.) повторяется в `packaging/interactive-workspace/AGENTS.md:52`, `packaging/po-workspace/AGENTS.md:153` и `skills/roles/ummanu/open-sprint/SKILL.md:240`. Это ≈40 % interactive-`AGENTS.md`, и он написан голосом PO для interactive-головы (DOC-12).

### 8.2 Расхождения

| ID | Документ | Расхождение | Приор. |
|---|---|---|---|
| DOC-02 | README, CONTRIBUTING, OPERATIONS, RECOVERY | Противоречивые инструкции установки: `pip install -e` (README:35, CONTRIBUTING:15) против `pip install .` (OPERATIONS:19–21, RECOVERY:638). Нигде не сказано создать `.venv` (см. BUG-02) | P0 |
| DOC-03 | `README.md:15-26` | В индексе нет `HEAD_SCOPES`, `OWNED_CLEANUP`, `REQUESTS_GROWTH`. Строка `:26` — «ummanu → ummanu»: артефакт скрипта переименования, должно быть «secretary → ummanu» (**✔**) | P3 |
| DOC-04 | `OWNED_CLEANUP.md:93` | Описан `--residue-replay --limit 20` и граница 1..100. Флага `--limit` нет; `cli.py:2094-2107` требует `--project` и `--target`/`--manifest` (не больше 20). `OPERATIONS.md:1681-1683` верен. Catch-up — мёртвый код (DEAD-08) | P2 |
| DOC-05 | `skills/roles/steward/steward/SKILL.md:3,135,172,175-181` | Написано «hourly», таймер — `OnCalendar=00/3:00:00`. Велено читать шаг `automations` у `upgrade --dry-run`, но такого шага нет: в `STEPS` 26 имён, `OPERATIONS.md:3126` подтверждает. В описаниях curate/retro/steward осталось «Launched by a session-manager automation» | P2 |
| DOC-06 | `skills/manifest.toml:50,83` | Codex-skills доставляются в `~/.config/orca/codex-runtime-home/home/skills`, хотя Orca-уровень `CODEX_HOME` удалён (A20 шаг 7), а Codex-головы используют `<data_dir>/codex-home` (`session.py:204`, `codex_home.py`). **Г:** Codex-головы interactive/curator/retro/steward не видят свои role-skills. Наблюдатели не затронуты (`observer.py:3373-3376` передаёт путь явно) | P1, если подтвердится |
| DOC-07 | `packaging/codex-home/AGENTS.md:5` | Сервер памяти назван `memory`, а это `LEGACY_SERVER`; живой — `po_memory` (`memory/client_config.py:22-23`) | P2 |
| DOC-08 | `packaging/memory/product-ummanu/sprints-and-reservations.md:8` | Факт «execution card принадлежит открытому спринту, override — исключение» противоречит текущему контракту (`PROTOCOLS.md:813` «Cards outside a sprint»; `po-workspace/AGENTS.md:19`). Через `memory_search` этот факт попадает во все головы. При правке обновить digest манифеста | P2 |
| DOC-09 | `HEAD_VITALITY.md` | Устаревшие имена: `DispatcherRuntime._trigger_wait_watchdog`/`_sigcont_head` (`:430,481`) теперь функции модуля; `_reduce_and_store_vitality_episode` стал публичным; `dispatcher_watchdog`/`dispatcher_tui` → `dispatch.watchdog`/`dispatch.tui` (то же в docstring `head_vitality.py:282,341`). Ложное утверждение `:429-430`: «`_stop_worker_confirmed` … run only beneath a guarded entry» — на деле 25 точек вызова, из них охраняются 4. Таблица rung 1 расходится с CON-18 | P2 |
| DOC-10 | `BOARD_STORE.md` | Ревизии «0001–0027», а head — 0030. CHECK `task_type` без `wait`. «nine owner event kinds», а их 14. Нет DDL для `po_sessions`/`po_turns`/`po_feed`/`po_requests`/`owner_events`/`origin_returns`. `body` будто бы без `[marker]`, хотя все три писателя хранят полный текст. Предикат charge не упоминает `e2e_refusal`. `committed` будто бы что-то значит. В коде: `schema.py:20` «eight jsonb columns», а их 11; «eleven-method vocabulary» (`sql_cards.py:5`), а их 14/18; `backend.py:174` называет `n` в `entity_id` `task_number`, а на деле это `board_key`. Триггер 0028 описан только в миграции | P3 |
| DOC-11 | `OPERATIONS.md:3087-3107` | Таблица шагов upgrade — 19 шагов, в коде 26 (нет `runtime-owner`, `po-workspace`, `pipeline-state`, `po-workspace-owner`, `po-token`, `web-front-config`, `po`). `:2420` документирует метку «reset already passed», которую тест `test_web_status_bar.py:863` запрещает | P3 |
| DOC-12 | `packaging/interactive-workspace/AGENTS.md` | Инструкции полномочий PO («The PO can supply… `--role po`») в контексте interactive-головы | P2 |
| DOC-13 | `ARCHITECTURE.md` | `:18` в списке пакетов нет `transition` (**✔**). `:37` пробы реестра включают `openrouter`, а `heads.toml` поставляет только `claude-sub`/`openai-sub`. `:345` список страниц без `/po`, `/doctor`, `/owner-events`. `:375` и `PROTOCOLS.md:4622` «`basicauth *` covers every path», а Caddyfile содержит cookie-bearer bypass плюс `handle { route { basicauth } }` (`caddyfile.py:116-136`) | P3 |
| DOC-14 | `TESTING.md`, `tests/README.md`, `tests/broad.py`, `CONTRIBUTING.md` | `TESTING.md:168` относит `test_web_transport`/`read_protocol`/`run_protocol` к `unit`, а в манифесте они `integration-board`. `:117` про PTY в `runtime-component` противоречит `:186`. `:28` «never a green skip», но `test_memory_service.py:17,234` и `test_memory_health.py:245` пропускают зелёным. `broad.py:9-10` пишет «1440 tests / 3782 tests», а статически сейчас 3 131 и 7 335; локальный прогон дал 3 136 (**✔**). `broad.py:28` и `CONTRIBUTING.md:27` упоминают Orca. `tests/README.md` не перечисляет `TMPDIR`-guard, `TA_CODEX_HOME`, `GIT_CONFIG_*`. `test_head_vitality_legacy_path.py:8` обещает `expectedFailure`, которого нет | P3 |
| DOC-15 | Мелкое | `pyproject.toml:141` «Portable ummanu appliance CLI skeleton.»; `SECURITY.md:30` («previously read runtime.env», «gitignored» — экспорт теперь по allowlist, `RECOVERY.md:389-399`); `examples/instance/instance.yaml:20` ссылается на несуществующий `ummanu-pipeline.service`; `packaging/systemd/README.md:3` без `ummanu-doctor.*`; `ROADMAP.md:10-16` числит в остатке, по-видимому, уже поставленные пункты (**Г**); `heads.toml:9-10,19-21` упоминает несуществующие `render_<adapter>` и `worker.py`; `head/local_pty/__init__.py:1-15` «no HeadRuntime here» | P3 |

### 8.3 План сжатия документации

| Действие | Сокращение | Ограничения |
|---|---|---|
| `RENAME.md`: оставить 3–5 тыс. «Old-name policy» (классы H/I/R/T и правила T5, которые исполняют `old_name_guard`/`config_check`), перенести в `OPERATIONS`/`ARCHITECTURE` | −56 тыс. | Сначала обновить ссылки §T1/§T3 из кода (`entrypoint_guard.py:26`, `production_checkout.py:15`, `upgrade.py:282`) |
| `HEAD_RUNTIME.md`: таблицу parity и чек-лист A20 заменить пятью строками; «Runtime scopes» → `HEAD_SCOPES.md` | −17 тыс. (37 → 12 тыс.) | — |
| Удалить `OWNED_CLEANUP.md`, абзац close/observer handoff — в `OPERATIONS` | −7 тыс. | — |
| `REQUESTS_GROWTH.md` и `OUTCOME_LINEAGE.md` влить в `BOARD_STORE`/`PROTOCOLS` | −3 тыс. | — |
| Удалить ≈40 инлайн-ссылок на карточки и PR и примеры со старыми ref (`OPERATIONS.md:1498-1500,2979,3001-3002`; `PROTOCOLS.md:2110-2113,4660`) | −10…15 тыс. | Часть строк закреплена тестами |
| Пересказ кода (имена внутренних функций, порядок блокировок) из `HEAD_VITALITY`/`BOARD_STORE`/`PROTOCOLS` перенести в docstring модулей; в docs оставить наблюдаемый контракт | −80…120 тыс. | Делать **вместе** со сжатием docstring (ARCH-01), иначе знание пропадёт с обеих сторон |
| Разбить `PROTOCOLS.md` на `docs/protocols/{tasks,sprints,e2e,po,web,memory,pipeline-reads}.md` с индексом 2–3 тыс.; `OPERATIONS.md` — по семействам runbook | контекст −80…95 % на вопрос | Рантайм цитирует заголовки (`release_lifecycle.py:194,516`, `release_activation.py:224`, `entrypoint_guard.py:26`); 37 тестовых модулей читают текст docs; `test_architecture` запрещает старые имена в `docs/` |
| Prompt: блок owner decisions — один источник (PO `AGENTS.md`), в `open-sprint` — указатель, из interactive — удалить; `observe-sprint` (33 тыс.) → ≤ 20 тыс., без грамматики NL-классификатора (`SKILL.md:536-552`) и анекдотов (`:325`); `steward` → ≤ 14 тыс. | −4 тыс. всегда загружаемого контекста; −13…20 тыс. у наблюдателя | Prompt поведенчески значим, нужны поведенческие проверки |

Итоговая цель: docs ≈973 тыс. → ≈650 тыс. (−33 %), `PROTOCOLS` ≤ 220 тыс. в файлах по 15–35 тыс., `OPERATIONS` ≤ 150 тыс. (**О**).

### 8.4 Упаковка, скрипты, CI

- **package-data** полон: JSON-схемы, `script.py.mako`, `heads.toml`, `docker-bin/docker`, 4 `automation.toml` (**Ф**).
- **mypy**: 65 файлов в списке, все существуют, но список ручной.
- **Зависимости:** `referencing` импортируется напрямую (`config.py`), но объявлен только транзитивно через `jsonschema>=4.18`; `pyte` (в скрипте) не объявлен.
- **CI-01**, подробно:
  - ruff нет в CI: 461 замечание, 344 файла не отформатированы (**✔**);
  - нет `cache: pip`;
  - нет `concurrency` и `permissions`;
  - агрегирующий `test` ставит весь пакет `.[ci]` только ради `scripts/ci_test_shards.py`;
  - `typecheck` не входит в агрегат `test`. Обязателен ли он в branch protection, из репозитория проверить нельзя;
  - checkout, setup-python и pip повторены 3 раза; composite action уберёт ≈25 строк;
  - `e2e-synthetic.yml` назван временным (sprint:1469).
- **`scripts/ummanu-agent-gate.sh`** на каждый пропущенный тик запускает `dispatch --cleanup-only`, который его же комментарий называет «no-op exit 0 for every agent»: лишний процесс Python на каждый тихий тик.

## 9. Явные баги и явные неэффективности

Этот раздел отделён от архитектурных рекомендаций. **Исправлений нет**: только описание и доказательство.

### 9.1 Баги

**BUG-01 — P0. Request smuggling после 413 на keep-alive.**
- Метка: Ф, воспроизведено субагентом. ✔ код подтверждён.
- Где: `web/server.py:182-188`, `protocol_version = "HTTP/1.1"` (`:138`).
- Что происходит: когда `_read_body` бросает `ValueError` (тело больше 64 КБ), обработчик пишет 413 и выходит. Тело он не дочитывает и `close_connection` не выставляет.
- Как воспроизведено: `POST /api/x` с телом 65 546 байт, начинающимся с `GET /smuggled HTTP/1.1…`. В ответ пришли 413, а затем `HTTP/1.1 200 … handled GET /smuggled`.
- Последствие (Г): контрабандный запрос несёт заголовки атакующего. Без `Origin` он проходит `cross_origin_reason` (`app.py:1283`). Поэтому межсайтовая форма на loopback может дойти, например, до `POST /api/pause/drain`. За Caddy возможна путаница ответов в пуле upstream-соединений.

**BUG-02 — P0. Свежая установка по документации не даёт рабочего хоста.**
- Метка: Ф.
- Все юниты запускают `{{UMMANU_PRODUCT_ROOT}}/.venv/bin/…`.
- Ни `bootstrap`, ни `install` не создают `.venv`. Его создаёт только подсказка transition (`transition/steps.py:401`).
- `upgrade.step_dependencies` пропускает работу, если `.venv` нет (`upgrade.py:578-581`).
- `_snapshot_install` (`upgrade.py:368`) считает не-editable установку дрейфом, а `OPERATIONS`/`RECOVERY` предлагают именно её.
- На Ubuntu 24.04, единственной поддерживаемой ОС, системный `pip install` отвергается (PEP 668).

**BUG-03 — P1. Профиль `--fast` не проходит на HEAD.**
- Метка: Ф ✔, воспроизведено оркестратором.
- `FAST_MODULES` (`scripts/ci_test_shards.py:36`) включает `tests.test_hermetic_board`. Его тест `test_a_test_can_still_opt_in_to_a_real_sprint_boards_shape` (`:59`) идёт через `sql_backend_fixtures.PostgresBoard.shared()` → `docker` (`:44`).
- Guard профиля запрещает внешние процессы, поэтому результат `RuntimeError: fast test profile forbids external command execution`, `FAILED (errors=1)`, `EXIT=1`.
- `test_ci_shards.py:216` проверяет только форму списка, поэтому поломку ничто не ловит.

**BUG-04 — P2. `ummanu pause --reason-file <нет такого файла>` падает с traceback.**
- Метка: Ф ✔.
- `run_pause` вызывает `_read_optional` (`dispatch/commands.py:188`), тот бросает `DispatcherError` (`:280`) вне обработчика.
- `cli.main` (`cli.py:169-174`) ловит только `MissingDefaultInstance`.

**BUG-05 — P2. `ummanu sprint show` падает с traceback при `instance.yaml` без пригодного `data_dir`.**
- Метка: Ф ✔.
- `resolve_data_dir(args)` вычисляется вне `try` функции `_read` (`sprint_commands.py:432-438`).
- Получается необработанный `TaskError` и код 1. Остальные команды в этой ситуации отдают JSON-ошибку и код 2.

**BUG-06 — P3. `ummanu gate` (онбординг) падает на `result.json`, который не является объектом.**
- Метка: Ф ✔.
- Строка `src/ummanu/gate.py:97` вызывает `candidate.get(...)` без `isinstance`-защиты, которая есть в строке `:95`.
- Строки `:133` и `:135` вызывают `previous.get(...)` без защиты (сравните с `:85`).
- Для пустого файла `load_config` возвращает `None`, получается `AttributeError` вместо исхода «conflict».

**BUG-07 — P2. Устаревшая запись диспетчера роняет весь дашборд в HTTP 500.**
- Метка: Ф по коду.
- `reads._records` → `DispatcherRecord.from_json` бросает `DispatcherError` (`dispatch/state.py:1166`).
- `_agents` ловит только `SOURCE_FAILURES` (`sources.py:56`), граница — только `IMPLEMENTATION_FAILURES` (`boundary.py:60`).
- Падают `/`, `/projects`, `/projects/{p}`, `/api/system`. Сам `sources.py:49-53` фиксирует, что этот дрейф исправлен только в `pause_reads`.

**BUG-08 — P3. Повреждённый конверт секрета выходит наружу как `ValueError`.**
- Метка: Ф, воспроизведено.
- `open_value` ловит только `InvalidTag/KeyError/TypeError` (`secret_store.py:395-421`).
- Неверная длина nonce или KDF `length≠32` даёт `ValueError`, а `_stored_value` (`:1066`) ловит только `SecretStoreStateError`.

**BUG-09 — P2. Два парсера frontmatter памяти расходятся.**
- Метка: Ф ✔, воспроизведено.
- Писатель `memory_write._split_fact` (`:364`) ищет строку `"\n---\n"`. Индексатор `memory/canon.parse_frontmatter` (`:91-96`) режет по первым двум сырым `---`.
- Пример: `title: a---b` писатель сохраняет как `{'title':'a---b'}`, индексатор читает как `{'title':'a'}`, а остаток метаданных попадает в тело.
- Незакрытый frontmatter даёт `ValueError` в индексаторе.
- Третья копия — `memory_journal._memory_fact_metadata:387`.

**BUG-10 — P2. Steward ищет осиротевшие workspace в Orca-корне.**
- Метка: Ф ✔; оговорка — конфигурация хоста.
- `steward/signals.py:42,452-470` сканирует `shared_state.WORKSPACES_ROOT`, то есть `TA_WORKSPACES_ROOT` или `~/orca/workspaces` (`shared_state.py:8`).
- Карточные worktree теперь лежат в `<data_dir>/workspaces/<project>/<worker>` (`dispatch/git_workspace.py:23-31,64-67`).
- Юнит `ummanu-steward.service` не задаёт `TA_WORKSPACES_ROOT`, поэтому `new_orphan_workspaces` слеп к текущим workspace.

**BUG-11 — P1, если подтвердится. Повторный подъём головы на том же `run_id` с устаревшим `head.pid`.**
- Метка: Г, сильные основания в коде.
- Цепочка:
  1. Постоянные роли переиспользуют `run_id` (`automations/runtime/dispatch.py:824`), а `head.pid` не удаляется.
  2. `client._identity_written` (`client.py:294-306`) принимает старую запись: её `run_id` совпадает.
  3. Голове, которой prompt передаётся после старта (Codex TUI: `curator` → `codex-sol-medium`, `steward` → `codex-sol-high`), `_rehydrate` проверяет `_identity_says_dead(address)`, даже когда supervisor только что ответил «alive» (`local_pty_head.py:1667`).
  4. Старый pid мёртв → admission закрыт с `DELIVER_HEAD_ENDED` → prompt отвергнут → `_abandon_bring_up` останавливает здоровую голову.
- Существующий тест `test_a_dead_head_is_an_ordinary_bring_up` (`tests/test_automations_dispatch_local_pty.py:700`), вероятно, этот путь не проходит.

**BUG-12 — P2. Ремонт pending-create пишет метаданные иначе, чем обычный create.**
- Метка: Ф по коду; достижимость — Г.
- `tasks.py:5877-5920` пишет метаданные без `touches_production` и `record_type="task"`; обычный путь (`1861-1901`) пишет оба поля.
- Отремонтированная operation-карточка теряет маркер production.

**BUG-13 — P2. `install`/`recover` перестраивают индекс памяти моделью по умолчанию.**
- Метка: Ф, трассировка кода.
- `installation.py:2152` и окрестности `:1880` берут `intfloat/multilingual-e5-large` (1024) и игнорируют `host.memory_model`.
- Юнит (`host_apply.py:404`) и `restore memory-reindex` (`restore_commands.py:213`) модель учитывают.
- Итог: несовместимый индекс и повторный полный embed.

**BUG-14 — P2. Ежедневный `ummanu-instance-maintenance` падает на штатной раскладке.**
- Метка: Ф ✔.
- `infra/instance_maintenance.run` начинается с `state_repo.require_repo(instance_dir)` (`:254`), а live root по умолчанию — обычный каталог.
- `RECOVERY.md:448-450` документирует exit 1. Побочные эффекты, по-видимому, не задуманы: Docker-cleanup после этого не выполняется, bare snapshot repo не пакуется, юнит постоянно в состоянии `failed`.

**BUG-15 — P3. Ложное «advancing» после respawn.**
- Метка: Ф асимметрия; влияние — О.
- `wait_vitality.py:1062-1066` передаёт предыдущий **provider**-курсор без проверки run. Курсоры child и journal к run привязаны (`:1067-1079`), а `head_status` привязывает все.
- На первом тике после respawn курсор нового run сравнивается со старым, отсюда ложные `ADVANCING`/`HealthyActive`.

**BUG-16 — P3. Подтверждение остановки может принять выход прошлой инкарнации.**
- Метка: Ф; достижимость — Г.
- `_await_head_gone`/`_has_exited` (`local_pty_head.py:2392-2410,3133-3141`) принимают любую мёртвую запись или любой `run.exited` в хвосте журнала, без нижней границы `run.started`.
- В переиспользованном каталоге run выход прошлой инкарнации подтверждает остановку текущей.

**BUG-17 — P3. `_set_observer_state` теряет `reason`.**
- Метка: Ф ✔.
- `observer.py:2079-2085` принимает `reason`, но только сравнивает его с `idle_reason`. Причины degraded/waiting (например, `:800`, `:1901`, `:1935`) не сохраняются.
- `last_action_at` обновляется каждый тик и означает «последний тик», а не «вход в состояние».

**BUG-18 — P3. Gate-фаза vitality возвращает устаревший эпизод.**
- Метка: Ф расхождение; влияние — Г.
- `gate_lifecycle._worker_vitality_for_gate` (`836-878`), когда ничего не наблюдалось, возвращает `record.worker_vitality_episode` (`:866`). Комментарий и docstring при этом обещают `None`.
- Политика SIGCONT/эскалации повторно отрабатывает по вердикту прошлого тика.

**BUG-19 — P3. Freeze может оставить worker работающим.**
- Метка: Г.
- `pause_ops.py:450-454`: при `HostError` во время остановки ревьюера `continue` пропускает и остановку worker этой карточки.
- Ref может дважды попасть в `stopped_reviewer` (`:446`, `:456`).

**BUG-20 — P3. Шрифты не грузятся из-за собственной CSP.**
- Метка: Ф.
- `pages.py:530,897-898` подключают Google Fonts, а CSP (`server.py:293`) — `default-src 'none'; style-src 'unsafe-inline'`.
- Шрифты не загружаются, каждая страница пишет CSP violation.

**BUG-21 — P3 (безопасность, узко). `_read_value` не сверяет `envelope["id"] == secret_id`.**
- Метка: Ф, проверено.
- Где: `secret_store.py:1053`.
- Тот, кто может писать в экспорт или remote, может поменять местами `values/a.enc.json` и `b.enc.json`.
- `_derive_key` берёт `n/r/p/length` scrypt из экспортного файла без ограничений.
- `_write_key_file:336` использует фиксированное временное имя без `O_EXCL`/`O_NOFOLLOW` (под lock, риск низкий).

**BUG-22 — P3 (тесты).**
- `test_head_memory.py:361-368`: счётчики `before`/`child_only` строятся и не используются; сценарий из имени теста не проверяется (Ф).
- `test_status.py:637`: код выхода `doctor` захвачен, но не проверяется (Ф).
- `test_dispatcher_tui.py:463` читает `~/.claude/projects` хоста, в CI всегда пропускается и нарушает контракт `tests/README.md:3-4` (Ф).
- Модульный `os.environ.setdefault("UMMANU_DISPATCHER_BODY_DIR", …)` в 5 модулях `test_head_vitality_*` даёт значение на весь шард по порядку импорта (Ф).
- Стенные часы в утверждениях: `test_ci_shards.py:338`, `test_doctor_record.py:341`, `sleep(0.5)` в `test_local_pty_dispatcher_launch.py:188` (Г, флейки).
- Тесты только для root (`test_github_credential.py:477`, `test_installation.py:201,1565`, `test_memory_health.py:185`) в GitHub CI не выполняются (Ф по конфигурации CI).

**BUG-23 — P3 (герметичность). Тесты зависят от login-профиля хоста.**
- Метка: Ф ✔, наблюдалось.
- Локальный `python -m tests.broad`: 3 136 тестов, 55 failures, 2 errors.
- Не меньше 44 сообщений об ошибке — лишняя строка `nvm` в захваченном stdout, например `'nvm\nnative stdout\n' != 'native stdout\n'`. Источник: `~/.bashrc` контейнера, который подтягивается через `bash -l`/`-lc` (`runtime/role_env.py:507`, `broad_check.py:287,301`, `dispatch/gate.py:400`, `dispatch/host.py:4834`).
- Остальные падения связаны с окружением: не-editable установка даёт `production runtime provenance refused`. Детально не разбирались.
- Это противоречит заявлению «unit suite is hermetic» (`CONTRIBUTING.md`, `tests/README.md`).
- Для продукта (Г): вывод login-профиля пользователя установки попадает в stdout gate и ролевых команд.

**BUG-24 — P3 (мелкие).**
- `board/sql_cards.py:1294` `_rpc_createComment` пишет `None` в NOT NULL при неверном времени; неизвестный column id даёт голый `KeyError` (`:949,1001`) (Ф).
- `_rpc_removeTask` шлёт любой ключ в `sprints.remove` и выдаёт вводящую в заблуждение ошибку (`:1300-1303`) (Ф).
- `head_vitality_episode._finite_timestamp` (`238-245`) не ловит `OverflowError` (Ф).
- `report()` в 4 слоях webproto даёт обрезанное «does not validate: » при пустом списке ошибок (Ф).
- `sprint_ops._reads()` без `owner_events`, поэтому `attention` при create всегда `unknown` (Ф).
- `config.py:196-203` повторяет одно сообщение схемы 4 раза (Ф).
- Supervisor считает таймеры по `time.time()` (`supervisor.py:610-635,1159,1262`), а не по монотонным часам (Ф).
- `host.teardown` упадёт на `task_ref: null` (`host.py:2787-2789`) (Г).
- Предсказуемые `/tmp/ummanu-<kind>-<ref>-<round>.md` (`host.py:4972`) на общем хосте (Г, безопасность).
- `memory/access.py:325` закрывает fd после `os.fdopen` (двойной close; Г).
- `checkpoint._SnapshotAudit._blobs` (`:2337`) делает `payload.index(b"\n")` и при усечённом выводе бросает необработанный `ValueError` (Г); близнец в `snapshot_tree.py:269` устойчив.
- `backup.py:129,141,171`: staging во временном каталоге системы и `os.replace` в `<data>/backups` дают `EXDEV` при `/tmp` на tmpfs или `PrivateTmp` (Г).

### 9.2 Неэффективности

| ID | Где | Суть | Метка |
|---|---|---|---|
| INEF-01 | `dispatch/runtime.py:994-1002`, `cleanup.py:383-396` | `save_records` в реальном режиме на **каждую** запись с workspace делает `reader.show(ref)` (чтение доски) и `CleanupOwner.remember` (≈6 `git rev-parse` плюс чтение и запись журнала). 144 точки вызова. `observer_provider_progress` (`host.py:1655-1657`) запускает полное сохранение, хотя наблюдатель в этот payload не входит | Ф (масштаб — Г) |
| INEF-02 | `board/sql_cards.py:1191-1197`; `sql_audit.py:154-156,304-311`; `events.py:149-154`; `sql_host.py:391-452` | `_rpc_saveTaskMetadata`: 1 UPDATE плюс по UPDATE на каждый ключ (≈11 операторов на create). `SqlTaskAudit` до 3 раз читает одну строку `requests` по PK, `commit` повторяет stage. `marker_comment` — до 6 полных `TaskReader.show`, по ≈8 запросов каждый (≈−24 запроса на маркер возможны) | Ф |
| INEF-03 | `claim.py:626-651` | 42 бесполезных `committed_event` на каждую попытку claim (см. CON-04) | Ф |
| INEF-04 | `web/app._runs_or_reason` → `ops.run_list` (`ops.py:475-498`) → `RunStore.for_ref` (`runs.py:424-441`) → `run_state` (`ops.py:592-599`) | Рендер страницы карточки читает все run-записи всех карточек. На каждый run: валидация конфига, `observe` и **две идемпотентные записи в audit** (`publish_started`/`publish_finished`). Страница перезагружается каждые 30 с | Ф (стоимость — Г) |
| INEF-05 | `head/local_pty/client.py:138,234`; `journal.py:184-190`; `po/runner.py:282` | Журналы голов без ротации, переживают инкарнации; читаются целиком, в `spawn_head` — каждые 20 мс до 20 с, в `ScopedPoProcess.wait` — каждые 50 мс весь turn. Оценка роста ≈1,4 МБ/ч активного вывода | Ф (рост — Г) |
| INEF-06 | `tasks.py:982-986,677-682`; `4858,4915,5500,5524` | `TaskReader.show_id` делает два полных `SELECT … FROM tasks` (включая архив) плюс все product/issue. Платит каждый create под lock ссылок. `retire_done` — 4·N·M | Ф |
| INEF-07 | `wait_vitality.py:1123-1125,362-363,669`; `review_verdict.py:224,309`; `e2e_after_merge.py:260-282` | Production state переписывается 2–4 раза за wait-тик на карточку. `parks_for_decision` читает спринт дважды. `reconcile_after_merge` на каждом тике читает всю доску (`restore_snapshot`) | Ф / Г (масштаб) |
| INEF-08 | `checkpoint.py:2337` | См. BUG-24; менее устойчивый близнец `snapshot_tree._blobs` | Г |
| INEF-09 | `restore.py:1429,1491,1383`; `upgrade.py:1594,1887,2081,2462,2478` | `restore-postgres` проверяет архив дважды: каждый файл хешируется дважды, мультигигабайтный архив читается ≈4 раза. Один upgrade хеширует весь `src/` (≈7,5 МБ) до 7 раз | Ф |
| INEF-10 | `board/sql_sprints.py:35`, `sql_cards.py:251`, `sql_audit.py:108,559` | Разная точность времени (см. CON-08) | Ф |
| INEF-11 | `web/pages.py`, `webfront/caddyfile.py:86-138` | Каждая HTML-страница — ≈45 КБ (10,9 КБ в gzip) со всем CSS и JS. В Caddyfile нет `encode`. Обновление каждые 30 с: одна открытая вкладка ≈130 МБ/сутки | Ф (арифметика) |
| INEF-12 | `po/store.py:228-245` | `PoStore._transaction` открывает новое соединение Postgres и выполняет запрос schema-gate на **каждую** операцию | Ф |
| INEF-13 | `local_pty_head.py:1112-1160,2122` | `_await_settled`/`_await_idle`/`_await_turn` опрашивают `observe` каждые 0,25 с до 90+ с (новое соединение, `/proc`, хвост 64 КиБ и JSON). `_follow` опрашивает каждые 20 мс | Ф |
| INEF-14 | `scripts/ummanu-agent-gate.sh` | На каждый пропущенный тик — лишний процесс Python `dispatch --cleanup-only` (no-op) | Ф |
| INEF-15 | `webproto/owner_events.py:76-87` | `snapshot()` глубоко копирует весь список событий на каждый спринт в листинге: O(спринты × события) | Ф |
| INEF-16 | `board/sql_cards.py:1205-1238`, `sql_sprints.py:363-414,550-568`, `sql_product_issues.py:480-491` | N+1 вставки (малые N). Подойдут `executemany` или `unnest` | Ф |
| INEF-17 | `web/app.py:565`, `pages.py:1214` | Дашборд читает `limits` провайдеров и тут же делает `del limits` | Ф |
| INEF-18 | `cli.py:1-118` | Импорт 275 модулей на каждый вызов CLI (см. ARCH-12). Supervisor на голову — 44 модуля, +10 МБ (ARCH-11) | Ф |

## 10. Безопасный план будущих рефакторингов

**Общие правила для каждого шага:**
1. Один PR — одна категория изменений, без смешивания.
2. Полный exact-SHA CI зелёный.
3. Перед PR — ruff по изменённым файлам.
4. Откат делается revert-ом коммита.

**Универсальный критерий для чисто прозаических шагов.** AST модулей с удалёнными docstring должен совпасть до и после: `ast.dump` без `Expr(Constant(str))` в начале тел. Это механически доказывает, что поведение не изменилось.

Порядок учитывает зависимости. Баги из §9 исправляются **отдельными** PR **до** рефакторинга затронутых мест, а не внутри него.

| Шаг | Содержание | Зависит от | Совместимость и ограничения | Нужные тесты (характеризационные или регрессионные) | Граница отката | Критерий приёмки | Ожидаемое сокращение (О) |
|---|---|---|---|---|---|---|---|
| 0 | Исправления P0/P1 из §9: BUG-01, BUG-02 (решение по `.venv`), BUG-03, BUG-11 после подтверждения, BUG-10, DOC-06 после проверки на хосте | — | Поведенческие изменения; нужна договорённость с владельцем | Регрессионные: keep-alive после 413; повторный тик роли с prompt после старта над мёртвой головой; `ci_test_shards.py --fast` в CI | Один PR на баг | Тест воспроизводит проблему до фикса и проходит после | — |
| 1 | Инструменты: ruff check (только изменённые файлы) в CI; `combine-as-imports = true`, затем `ruff --fix` **только** I001 по затронутым пакетам; pip-cache, `concurrency`, `permissions` | — | Без изменения семантики; конфликты слияния с параллельными PR, поэтому делать пакет за пакетом | Полный CI | Каждый пакет отдельным коммитом | Ноль новых замечаний на изменённых файлах; diff — только импорты | −400…500 строк |
| 2 | Удаление мёртвого кода с низким риском: DEAD-01…08, 10, 12–21, 23, 24; DEAD-25 и DEAD-11 — с решением владельца | 1 | Публичные экспорты (`board.__all__`, `FakeBoardHost`) убирать вместе с тестами; на старые имена смотрит `test_old_name_guard` | Полный CI; `grep` на имя в репозитории = 0 | По одному коммиту на группу | Тесты зелёные; доказательство `grep` в описании PR | −1,8…2,5 тыс. строк, ≈−70…90 тыс. символов |
| 3 | Тестовые данные и помощники: TDEAD-01…04; TDUP-03/04 (`tests/support/process.py`, `tests/support/instance.py`, использование `support/git.py`); TDEAD-05 (листовые классы) | — | Ничего продуктового; число тестов меняется только на 1 дубль и 8 лишних прогонов | Сравнить множество имён тестов до и после (`unittest` discovery) | Коммит на файл помощника | Множество тестов то же (кроме удалённого дубля); покрытие не падает | −266 КБ данных, −650 строк |
| 4 | Листовые извлечения, ломающие циклы: `board/errors.py` (`TaskError`), `board/sql_rows.py`, `webproto/_values.py`, `head/procfs.py`, перенос `with_pid_heartbeat` в лист и ленивый `head/__init__` (ARCH-11), `infra.git_worktree` парсер | 2 | Старые имена реэкспортировать; цели патчей в тестах сохранить | Тест импорта (`python -c 'import …'` для каждого модуля); `-X importtime` для supervisor | Коммит на лист | Локальных импортов меньше; supervisor ≤ 20 модулей `ummanu` | −200…300 строк; −10 МБ RSS на голову |
| 5 | Волна сжатия прозы (ARCH-01) по пакетам: `webproto` → `runtime` → `dispatch` (vitality — после исправления DOC-09) → `board` → корень → `tests`. Историю инцидентов в docstring не переносить, она в git | 1 | Тесты, ищущие текст в исходниках (`test_web_status_bar.py:864`, `test_web_po_transport.py:627`, `test_architecture`), проверить заранее | Критерий AST-эквивалентности без docstring; полный CI | Пакет за пакетом | AST-эквивалентность; доля прозы в пакете ≤ 15 % | **−450…650 тыс. символов в `src`**, −250…350 тыс. в `tests` |
| 6 | Разбиения через mixin и модули без изменения API: `tasks.py` (сначала помощники, затем mixin), `dispatch/host.py`, `observer.py` (чистые разрезы), `sprints.py`, doctor из `cli.py`, `dispatch/gate.py` → `github_ci.py`, `local_pty_head` → пакет, `sprint_reads`, `web/pages` (сначала static, затем пакет), `upgrade`/`checkpoint`/`installation` → пакеты по целевой раскладке `ARCHITECTURE.md` (сократить `LEGACY_FLAT_MODULES`) | 4, 5 (проза меньше — diff меньше) | Реэкспорт всех используемых тестами имён; строковые цели `mock.patch` перенацелить там, где вызывающий переехал; обновить whitelist в `test_local_pty_head_runtime.py:2187` и `test_architecture.py:868`; package-data для `web/static` | Существующий набор плюс тест, что все прежние публичные имена импортируются из старых путей | Модуль за модулем | Крупнейший файл `src` ≤ 2 000 строк; поведение то же (полный CI) | ≈0 в объёме; контекст типовой задачи −60…85 % |
| 7 | Устранение дублирования со средним риском: DUP-D3 (`_PersistedMapping`), сериализация по таблице полей, DUP-I1 (`ProcessReceipt`), DUP-D11 (e2e), DUP-B6, DUP-W1/W4, DUP-D4 (таблица ролей, заодно CON-06), DUP-D9/D10/D19 | 6 | Байт-совместимость JSON на диске (порядок ключей, `sort_keys`); тексты сообщений и request id | **Golden round-trip** для `DispatcherRecord`/`ObserverRecord`/receipts по реальным образцам; снапшот-тесты текстов ошибок | Один дубль — один PR | Golden-тесты байт-в-байт | −1,2…1,6 тыс. строк, ≈−50…70 тыс. символов |
| 8 | Вывод одноразового и унаследованного кода (решение владельца): DEAD-T (`transition/`, скрипты, тесты, `RENAME.md`), ARCH-09 (live root как git work tree), DEAD-R (`reference_repair`), DEAD-22 (`attach`/`request_drain`), DEAD-H1 (`recover_*` после доказательства) | 2, 6 | Невозможность rollback перехода; восстановление с legacy-remote | Тест `old_name_guard` с новым allowlist; восстановление снапшота end-to-end (`integration-recovery`) | Каждое направление отдельно | Владелец подтвердил, что старых хостов и remote нет | −170…200 тыс. символов `src`+`scripts`, −83 тыс. тестов, −56 тыс. docs |
| 9 | Документация: удалить историю (§8.3), разбить `PROTOCOLS`/`OPERATIONS`, исправить DOC-02…15, сжать prompt-skills | 5 (пересказ кода переносится в docstring синхронно) | Заголовки, которые цитирует рантайм (`release_lifecycle.py:194,516`, `release_activation.py:224`, `entrypoint_guard.py:26`); 37 тестов, читающих docs; якоря | Проверка якорей и ссылок; тесты docs | Файл за файлом | Нет битых ссылок; docs ≤ 650 тыс. | −320 тыс. символов docs; −80…95 % контекста на вопрос |
| 10 | Разбиение тестовых файлов (§5.9) и снятие Docker-гейта с классов без Postgres | 3 | `ci-shards.txt`: каждый файл ровно один раз; история JUnit по старым классам прервётся | Валидатор манифеста; сравнение множества тестов | Файл за файлом | Крупнейший тестовый файл ≤ 40 тыс. токенов; 86+ тестов уходят из Docker-шарда | ≈0 строк; −145 тыс. токенов на крупнейший файл |

**Сводная оценка** (**О**, ±30 %):

| Область | Сейчас | После |
|---|---|---|
| `src` | ≈7,37 млн символов | ≈6,4–6,7 млн (−9…13 %) |
| `tests` | ≈9,4 млн | ≈8,8–9,0 млн (без вывода `transition`) |
| `docs` | ≈973 тыс. | ≈650 тыс. |

Главный выигрыш для агентов — не в сумме, а в контексте типовой задачи: в 2–7 раз меньше по `src` и в 5–20 раз меньше по docs.

## 11. Неопределённости, ограничения и отвергнутые абстракции

### 11.1 Ограничения аудита

- **Нет доступа к production-хосту.** Поэтому DOC-06, BUG-11, DEAD-H1, DEAD-R и масштабы INEF-* остаются гипотезами или оценками. Branch protection (обязателен ли `typecheck`) и адаптеры инстанса (`e2e-synthetic.yml`) из репозитория не видны.
- **Среда.** Системный `python3` в контейнере — 3.11, он не разбирает `web/pages.py` (f-string по PEP 701). Проверки шли во временном venv на 3.12 без editable-установки (`PYTHONPATH=src`). Поэтому часть падений `tests.broad` вызвана средой (§9, BUG-23). Интеграционные шарды с Docker и Postgres не запускались, mypy не запускался.
- **Полнота чтения.** Целиком прочитана только часть файлов; глубина указана в матрице §2.3. Остальное покрыто автоматическими проходами (AST, tokenize, `grep`, vulture, ruff) и чтением по наводкам. Поэтому отсутствие находки в файле с глубиной «А» не означает отсутствия проблем.
- **Природа оценок.** Все объёмы сокращения — оценки (±25–30 %), основанные на диапазонах строк, выбранных вручную, и на соотношении ≈4 символа на токен. Реальная токенизация зависит от модели.
- **Перепроверка.** Выводы субагентов я перепроверял выборочно (метка ✔). Остальные «Ф» опираются на их чтение кода со ссылками file:line на коммит `e310833`.
- **Сторонние артефакты.** В репозиторий ничего не записывалось, кроме этого файла. Интерпретатор оставлял `__pycache__`, который игнорируется `.gitignore` и в коммит не попадает.

### 11.2 Сознательно отвергнутые абстракции

- **Общий слой Repository/ORM для `board/sql_*`.** Модули работают на явном SQL с разной семантикой транзакций. Хватает листа `sql_rows.py` и нескольких чистых функций (§6.3 DUP-B1…B4); универсальный репозиторий добавит код без выигрыша.
- **Слияние `head_vitality`, `wait_vitality` и `head_status`.** У них разные роли: построение снимков, решение, read-only представление для оператора. Реально общие только DUP-D14 и DUP-D15.
- **Объединение `FakeBoardHost` с SQL-реализацией или «общий базовый класс доски».** Fake — тестовый двойник с другой семантикой (CON-12). Правильный ход — вынести его в `tests/fakes/`, а не обобщать.
- **Композиция вместо mixin на первом шаге разбиения `host.py`/`tasks.py`.** Композиция чище, но ломает сотни `patch.object(host, …)`. Mixin — безопасный промежуточный шаг, а композиция может быть следующим этапом.
- **Плагинный фреймворк для шагов `upgrade`.** Хватает спецификации `ProcessReceipt` для трёх реальных дублей; остальные шаги разнородны.
- **Общая база ошибок и таблиц `_CODES` для всех слоёв `webproto`.** Таблицы отображают разные исходные словари.
- **Общие `$defs/source` для всех `web-*` схем через `$ref`.** Это технически возможно (`config._schema_registry`), но меняет файлы контрактов при малом выигрыше; низкий приоритет.
- **Устранение копий в миграциях Alembic.** Миграции должны быть заморожены. Наоборот, `0004` надо **заморозить** (убрать импорт живого `record_key`).
- **Устранение копии в `runtime_preflight.py` и встроенного heartbeat-writer.** Это требование stdlib-only и автономного запуска.
- **Дробление ради метрики строк.** Многие маленькие модули `board/*` — связные листья с двумя и более импортёрами (`extension_bag` — 15, `roles` — 13, `local_run` — 9). Их не надо сливать или дробить дальше.
- **DI-контейнер или реестр сервисов вместо 17 «утиных» проб.** Проще дополнить тестовые фейки и удалить пробы.

### 11.3 Главные гипотезы, требующие проверки (по убыванию ценности)

1. **BUG-11.** Повторный тик роли с prompt после старта над мёртвой головой: нужен тест с профилем Codex TUI.
2. **DOC-06.** Видят ли Codex-головы свои role-skills: `ls <data_dir>/codex-home/skills` на хосте и тик Codex-steward.
3. **DEAD-H1.** Остаются ли протокольные `requests` в статусе staged в production: `settle_stale_staged`, `oldest_pending`.
4. **INEF-01 и INEF-04.** Профилирование тика и рендера карточки на реальном числе активных карточек и runs.
5. **CON-03 и CON-18.** Намеренны ли `status:"ok"` при блокировке и потеря ESCALATE на healthy-тиках: вопрос к владельцу контракта.
6. **BUG-19 и BUG-24 (Г-часть).** Проверка тестами отказов.

## 12. Проверка на production-хосте (2026-10-05, прод на 6b0d85c7)

Проверка шла только на чтение: `SELECT` под ролью `ummanu_read` с `default_transaction_read_only=on`, файлы в `~/ummanu-data`, `systemctl show/cat`, `journalctl`, `gh api` GET и несколько `curl` GET к `127.0.0.1:8787`. Сервисы не перезапускались, в доску и в инстанс ничего не писалось. Секреты в отчёт не попали.

**Ревизия прода.** `/home/dev/ummanu` HEAD — `6b0d85c7` (merge PR #667), рабочее дерево чистое. От `e310833` отличается одним файлом, `tests/test_local_pty_supervisor.py` (+13/−8), поэтому все ссылки file:line из §1–§11 верны и для прода.

**Окно истории.** Журналы systemd под нынешними именами юнитов начинаются 2026-10-02 08:38 (переименование secretary → ummanu). У `secretary-web.service` журнал есть с 2026-09-25. Аудит `requests` хранится с 2026-07-13, `runs.jsonl` ролей — с 2026-08-04, Codex rollout — с 2026-09-24.

### Сводка

| Пункт | Вердикт |
|---|---|
| DOC-06 | **подтверждено** (curator) |
| BUG-11 | **опровергнуто по истории** (2 из 2 реальных повторных подъёмов прошли) |
| DEAD-H1 | **подтверждено** (staged-строк не было и нет; settle и recover работы не находили) |
| DEAD-R | **подтверждено** (дублей нет, pending-ремонтов нет) |
| BUG-10 | **подтверждено**, плюс вторая слепая зона: регэксп имён |
| BUG-14 | **подтверждено** (failed с 2026-10-05) |
| BUG-13 | **опровергнуто для этого хоста** (настроена модель по умолчанию) |
| BUG-20 | **подтверждено** |
| BUG-23 (продукт) | **опровергнуто для этого хоста** |
| BUG-07 | **опровергнуто по истории** (0 ответов 500 с 25.09) |
| BUG-12 | **опровергнуто по истории** |
| CON-08 | **подтверждено** (форматы сосуществуют); влияние — сравнение в пределах одной секунды |
| CON-09 | **опровергнуто для этого хоста** |
| BUG-24 EXDEV | **опровергнуто для этого хоста** |
| INEF-01/04/05/07/11/12/13 | масштаб измерен (12.14). Главное: `cleanup.json` 37,5 МБ переписывается ≈91 раз за 5 мин; тик 05.10 — 100–466 с при 72 % CPU |
| Branch protection, `e2e-synthetic` | `main` не защищена; адаптера нет |

### 12.1 DOC-06: видят ли Codex-головы свои role-skills

**Что проверено:**
- `ls -la <data>/codex-home/skills` и `~/.config/orca/codex-runtime-home/home/skills`;
- `skills/manifest.toml` и `heads/heads.yaml`;
- `/proc/<pid>/environ` живых Codex-голов;
- каталог `## Skills` во всех rollout `<data>/codex-home/sessions/**`: 1 483 файла, из них 349 TUI.

**Найдено (Ф):**
- В `<data>/codex-home/skills` есть только `.system`. В Orca-корне лежат все 9 role-skills, включая `curate`, `retro`, `steward`, `observe-sprint`, `open-sprint`, `knowledge-doc` и `grilling`.
- Живые Codex-головы запускаются с `CODEX_HOME=/home/dev/ummanu-data/codex-home`. Это видно у pid 377672/377679 (observer sprint-1479) и у curator.
- Сейчас Codex использует из ролей только curator: `role_defaults.curator = codex-terra-high-local-pty`. Steward и retro работают на Claude, их `.claude/skills/{steward,retro}` в workspace на месте.
- Все 9 TUI-сессий curator за 03–04.10 показывают один и тот же каталог: `Skill roots: r0 = <data>/codex-home/skills/.system` плюс plugin-кэш. Доступны только `imagegen`, `openai-docs`, `skill-creator` и `skill-installer`; `curate` в каталоге нет.
- Ни один rollout из 1 483 не содержит `curate`, `steward`, `retro` или `observe-sprint` в каталоге. Role-skills видны только PO (`codex_exec`, cwd `<data>/po`, корень `@po/.agents/skills`: `grilling`, `knowledge-doc`, `open-sprint`).
- Пример: сессия `rollout-2026-10-04T22-00-20-*`, prompt `$curate`. Голова сначала ищет инструменты по `/curat/`, затем `memory_search("… $curate")`, затем `rg -i curate` по своему cwd. Только после этого она пишет «I found the installed `curate` role procedure» и читает `skills/roles/curator/curate/SKILL.md`.
- Это работает лишь потому, что cwd curator (`~/orca/workspaces/ummanu/curator`) — git-checkout самого ummanu от 2026-10-02. Значит, голова читает версию skill из checkout, а не синхронизированную копию.
- Попутно (DOC-07, **Ф**): `codex-home/AGENTS.md` этой сессии велит звать сервер памяти `memory`.

**Вердикт: подтверждено** для curator, единственной Codex-роли на этом хосте. Interactive/retro/steward на Codex сейчас не настроены. Для них дефект латентный: при переключении профиля они так же не увидят skill.

### 12.2 BUG-11: повторный подъём на том же `run_id` с устаревшим `head.pid`

**Что проверено:**
- все 838 `heads/*/journal.jsonl`: поиск run-директорий с несколькими `run.started` и остановок с инициатором `head-launch` (сигнатура `_abandon_bring_up`, `local_pty_head.py:2292-2333`);
- `automation-state/{curator,steward,retro}/runs.jsonl`;
- journald ролей;
- `task_comments` на тексты `DELIVER_HEAD_ENDED` (`local_pty_head.py:366`) и «its prompt did not reach it».

**Найдено (Ф):**
- **Повторные подъёмы на том же run_id.** Их 5 run-директорий: curator `5f36bbcb` (×3, 24.09), `8c826d2e` (×2, 24.09 и 03.10 15:58), `c4ee912c` (×2, 04.10 12:41 и 22:00); steward `0af77284` (×6); retro `2e203b4c` (×2).
- **Случаи с Codex TUI.** Prompt после старта (`curator-dispatch` + `curator-dispatch:submit`) при мёртвом прошлом pid был дважды: `8c826d2e` 03.10 и `c4ee912c` 04.10 22:00.
  - В `c4ee912c` прошлая инкарнация остановлена `drain.requested po` в 21:40:50, без `run.exited`.
  - Новый `head.pid` записан в 22:00:18, prompt отправлен в 22:00:26. Оба `input.accepted` прошли, `turn.started` есть, в `runs.jsonl` записано `supervised-started` и `advance` в 22:03.
- **Остановки `head-launch`.** Их 0. `supervised-start-failed` и аналогов в `runs.jsonl` ролей тоже нет.
- **`DELIVER_HEAD_ENDED` в истории — 2 раза, оба на карточках, а не у постоянных ролей:**
  - `secretary-1713`, 24.09: reviewer, 10 неудачных запусков подряд;
  - `butler-12`, 02.10 17:38: worker, run `3b3a9012`. Run_id свежий, один `run.started`. Голова действительно завершилась через 0,8 с после submit (`turn.finished head_exited`, `run.exited`). Устаревший pid тут ни при чём.

**Почему не сработало (О):** `_identity_written` (`client.py:294-306`) действительно принимает старую запись. Но к моменту `_rehydrate` перед доставкой prompt (через ≈6–8 с после старта) launch-обёртка уже перезаписала `head.pid` новым живым pid. Опасное окно — только время между возвратом `start` и записью новой identity.

**Вердикт: опровергнуто по истории.** Оба реальных случая Codex-TUI на переиспользованном run_id прошли успешно. Узкая гонка в коде остаётся (**Г**), её закрывает тест из §11.3 п.1.

### 12.3 DEAD-H1: остаются ли протокольные `requests` в `staged`

**Что проверено:**
- `SELECT status, count(*) FROM requests GROUP BY 1`;
- распределение `settled_at - created_at` по `protocol`;
- поиск `stale_refusal`;
- журнал диспетчера на «unresolved pending record» и «stale staged».

**Найдено (Ф):**
- В `requests` 53 695 строк (2026-07-13 … 2026-10-05), все `committed`. `staged` — 0, `discarded` — 0.
- Задержка между `created_at` и `settled_at`:

  | Строки | Число | Максимум | Дольше 1 мин | Дольше 15 мин |
  |---|---|---|---|---|
  | протокольные | 14 842 | 5,2 с | 0 | 0 |
  | прочие | 38 853 | 1 мин 24 с | 2 | 0 |

- `settle_stale_staged` (`sql_audit.py`, грейс `STALE_STAGED_GRACE_SECONDS = 900`) коммитит только строки старше 15 мин, а отказ оставил бы `discarded`. Ни одной такой строки нет, значит работы он не находил ни разу.
- `oldest_pending` вызывается только для текста отказа checkpoint (`checkpoint.py:155,529`). Отказ «unresolved pending record(s)» в журнале диспетчера (с 02.10) встречается 0 раз.
- `recover_*` (`sql_host.py:464-660`) вызываются из `tasks.py:5255,5259,5383` и `product_issues.py:1036,1330,1401` только над staged-строкой. Логов нет, но и предусловия на хранилище не было ни разу.
- Оговорка (Г): транзиентная staged-строка (≤5 с) теоретически может попасть в `reconcile` соседнего процесса. Следов этого нет.

**Вердикт: подтверждено.** В production протокольные строки в `staged` не задерживаются, settle и recover работы не находили. Вывод машинерии из эксплуатации по этому критерию обоснован.

### 12.4 DEAD-R: дубли ссылок и pending `reference_repaired`

**Что проверено:** дубли `tasks` по `task_ref`, `(project_id, task_number)` и `board_key`; уникальные индексы `tasks`; строки `requests` с `operation='reference_repaired'`.

**Найдено (Ф):**
- В `tasks` 1 729 строк. Число различных `task_ref`, номеров и `board_key` тоже 1 729.
- Уникальность обеспечивают индексы `tasks_pkey`, `tasks_project_id_task_number_key` и `uq_tasks_board_key`.
- `reference_repaired` встречается 2 раза, оба `committed`: `secretary-1551` и `secretary-1552`, 2026-09-05 11:56, ещё на `backend.kind = kanboard`.
- Pending-строк нет.

**Вердикт: подтверждено.** Дублей нет, pending-ремонтов нет, а на PostgreSQL дубль невозможен по схеме. `board/reference_repair.py` можно выводить.

### 12.5 BUG-10: в каком корне steward ищет осиротевшие workspace

**Что проверено:**
- `systemctl cat ummanu-steward.service` и `packaging/systemd/ummanu-steward.service:13-17`;
- `runtime.env` (только ключи);
- `automation-state/steward/{runs.jsonl,watermark.json}`;
- содержимое `<data>/workspaces/*` против состояния карточек в `tasks`.

**Найдено (Ф):**
- Юнит и `runtime.env` не задают `TA_WORKSPACES_ROOT`, поэтому steward сканирует `~/orca/workspaces` (`shared_state.py:8`). Там нет ни одного каталога с pipeline-именем: только `ummanu/{curator,pipeline,retro,steward}` и один старый `codegen_orchestrator/codegen-orchestrator-1342-…`.
- `new_orphan_workspaces` равно 0 во всех 121 precheck с 2026-08-04 по 2026-10-05. `notified_orphans = []`.
- Реально в `<data>/workspaces` лежит 41 карточный workspace (без `observers/`). Из них 29 принадлежат `done`-карточкам (13 из них уже archived), 2 — archived `ready`-карточкам (`codegen-orchestrator-1456`, `-1488`). Ещё 8 — блокированные или в работе, то есть живые.
- **Вторая слепая зона (Ф, новая).** `_PIPELINE_WS_RE = ^(review-)?\d+-` (`steward/signals.py:448`) ждёт имя, начинающееся с цифр. Нынешние имена — `<project>-<n>-<slug>`. Регэксп совпадает с 0 из 41. Поэтому даже с правильным `TA_WORKSPACES_ROOT` сигнал останется слеп.

**Вердикт: подтверждено.** Сигнал сирот мёртв по двум причинам: корень и регэксп.

### 12.6 BUG-14: `ummanu-instance-maintenance`

**Что проверено:** `systemctl show` для service и timer; journald обоих имён юнита; `git count-objects -v` по `<data>/backup/*.git` (только чтение).

**Найдено (Ф):**
- Юнит `failed`, `ExecMainStatus=1`, последний запуск 2026-10-05 04:26:59 UTC: `{"error": "instance repo is not a git repository: /home/dev/ummanu-data/instance", "status": "failed"}`. Следующий запуск — 2026-10-06 04:20:58, он упадёт так же.
- До этого запуски шли успешно (`status: ok`) — `secretary-instance-maintenance` с 27.09 и `ummanu-…` 03.10 и 04.10. Тогда работали Docker-cleanup (например, 29.09: удалено 9 и 3 анонимных тома) и упаковка: 04.10 loose 10 495 → 5, packs 13 → 14. Instance тогда указывал на `/home/dev/secretary-instance`.
- Юнит переписан 2026-10-04 12:10. Instance стал обычным каталогом `<data>/instance` (live root, создан 04.10 13:44).
- Bare snapshot repo `<data>/backup/ummanu-instance.git` не упакован ни разу: 7 953 loose-объекта, 141 MiB, 0 packs. Последний коммит — 2026-10-05 23:17.

**Вердикт: подтверждено.** Падение началось с переходом на live root (1 падение на момент проверки). Docker-cleanup и упаковка с 05.10 не выполняются.

### 12.7 BUG-13: модель индекса памяти

**Что проверено:** `instance.yaml` (`host.memory_model` / `memory_dim`), `Environment` юнита `ummanu-memory.service`, `index_metadata` в `<data>/memory/index.sqlite` (открыт `mode=ro`), `manifest.json`.

**Найдено (Ф):**
- В конфиге `memory_model: intfloat/multilingual-e5-large`, `memory_dim: 1024`; юнит указывает то же.
- В индексе `model = intfloat/multilingual-e5-large`, `dimension = 1024`, `schema = 2`, 345 фактов, `vec0(embedding float[1024])`. Последняя пересборка — 2026-10-04 22:03.
- Настроенная модель совпадает с той, что зашита в `installation.py` и `memory/__init__.py:6`. Поэтому пересборка через install/recover здесь не меняет индекс, даже если она была.

**Вердикт: опровергнуто для этого хоста.** Дефект кода (Ф по трассировке) остаётся латентным для любой установки с другой моделью.

### 12.8 BUG-20: CSP и Google Fonts

**Что проверено:** `curl -D` для `/`, `/projects/ummanu` и `/tasks/ummanu-90` на `127.0.0.1:8787`; поиск ссылок на шрифты в HTML.

**Найдено (Ф):**
- Живой заголовок: `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'`.
- В каждой из трёх страниц есть `<link rel="preconnect" href="https://fonts.googleapis.com">` и `<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans…">` (`pages.py:530,897-898`).
- В политике нет ни внешнего `style-src`, ни `font-src`, поэтому таблица стилей блокируется, и шрифт откатывается на системный.

**Вердикт: подтверждено** по заголовку и разметке. Консоль браузера не смотрели, но семантика CSP однозначна.

### 12.9 BUG-23, продуктовая часть: login-профиль в stdout

**Что проверено:** `bash -lc 'echo MARK'` под `dev`; `~/.profile` и `~/.bashrc` на `nvm`; `grep nvm` по `instance/gate-runs` (16 файлов) и по worker-local receipts в `<data>/workspaces/*/*/.ummanu-task-env/checks`.

**Найдено (Ф):** login-shell печатает только `MARK`, в профилях `nvm` нет, совпадений в gate-runs и receipts — 0.

**Вердикт: опровергнуто для этого хоста.** Утечка из §9 BUG-23 — свойство контейнера аудитора. Негерметичность тестов при этом остаётся (Ф).

### 12.10 BUG-07: HTTP 500 от устаревшей записи диспетчера

**Что проверено:** access-лог `ummanu-web.service` (с 02.10) и `secretary-web.service` (25.09–02.10), разбор по статусам; классы трейсбеков; Caddyfile на наличие `log`.

**Найдено (Ф):**
- **ummanu-web:** 7 339 ответов 200, 199 — 303, 3 — 401, 1 — 503 (`GET /` 2026-10-04 10:59:52, это `backend_unavailable`, `statuses.py:41`).
- **secretary-web:** 16 799 ответов 200, 347 — 303, 3 — 400, 5 — 401, 2 — 404.
- Статуса 500 нет ни разу.
- Трейсбеки: 50 `BrokenPipeError`, 9 `ConnectionResetError` и 4 обрыва при обработке запроса — всё это клиентские разрывы. `DispatcherError` и `LegacyDispatcherRecord` не встречаются.
- В Caddyfile нет директивы `log`, поэтому access-лога фронта нет.
- Сейчас в `production-state.json` 3 записи `records`, и дашборд отвечает 200.

**Вердикт: опровергнуто по истории** (окно 25.09–05.10). Кодовый путь (Ф) остаётся.

### 12.11 BUG-12: operation-карточки после ремонта pending-create

**Что проверено:**
- `tasks.extensions->'extra'` по `task_type`;
- история `requests` у единственной operation-карточки без поля;
- `sql_cards.py:1110-1117` (чтение `record_type`).

**Найдено (Ф):**
- Operation-карточек 74 (2026-09-26 … 2026-10-05). У 73 есть `touches_production`.
- Без поля только `secretary-1763`. Она создана 2026-09-26 15:47:49 обычным путём: строка `created` закоммичена через 1,7 мс, актор observer. Первая карточка с полем появилась в 18:11 того же дня, то есть `secretary-1763` старше самого поля.
- Ремонт pending-create требует задержавшейся staged-строки, а таких не было (12.3).
- `record_type="task"` на PostgreSQL выставляет чтение для любой строки `tasks` (`sql_cards.py:1117`), поэтому эта половина находки на SQL-бэкенде не проявляется.

**Вердикт: опровергнуто по истории.** Ремонтных create не было. Расхождение кода (`tasks.py:5877-5920`) остаётся, но достижимо только из staged-строки.

### 12.12 CON-08 и CON-09: форматы времени и env-файлы

**Что проверено:**
- типы всех колонок `*_at`;
- доля дробных секунд по таблицам;
- формы строк-времени в `requests.intent`, `tasks.extensions` и `production-state.json`;
- форма строк `runtime.env`, `board-store.env`, `webfront/owner-password.env`: счётчики `export`, кавычек, `$`, `\`, пробелов и расхождений с `shlex.split`. Значения не печатались.

**Найдено (Ф):**
- Все колонки времени в БД — `timestamptz`, поэтому сравнение в SQL типизировано.
- Точность смешанная:

  | Колонка | Дробные секунды |
  |---|---|
  | `board_events.occurred_at` | 14 844 из 14 844 |
  | `requests.created_at` | 31 661 из 53 702 |
  | `task_comments.created_at` | 10 816 из 26 939 |
  | `tasks.created_at` | 747 из 1 729 |
  | `sprints.created_at` | 45 из 149 |

- В JSON-тексте формы сосуществуют:
  - `requests.intent` (`at` / `occurred_at` и т. п.): `…:SSZ` — 38 858, `…:SS.ffffffZ` — 14 844;
  - `tasks.extensions`: 470 и 186;
  - `production-state.json`: 200 и 2;
  - `runs.jsonl` ролей: `…+00:00` (374).
- `chargeSprintE2e(at=isoformat())` пишет в `sprint_e2e_charges.charged_at timestamptz` и поэтому безвреден.
- Строковое сравнение `Z` с `.fZ` и `+00:00` ошибается только в пределах одной секунды.
- Env-файлы. `runtime.env`: 1 строка `KEY=VAL` и 2 комментария. `board-store.env`: 9 строк `KEY=VAL`. `owner-password.env`: 1 строка. Ни `export`, ни кавычек, ни `$` или `\`, ни расхождений с `shlex`.

**Вердикт:** CON-08 — **подтверждено** (сосуществуют), влияние ограничено одной секундой (**О**). CON-09 — **опровергнуто для этого хоста**: три парсера читают эти файлы одинаково.

### 12.13 BUG-24: EXDEV у backup

**Что проверено:** юниты `*backup*`; `PrivateTmp` у всех юнитов `ummanu*`; `findmnt -T /tmp` и `-T <data>`; `<data>/backups`.

**Найдено (Ф):**
- Юнитов backup нет, бэкап запускается вручную.
- `PrivateTmp=yes` нет ни у одного юнита.
- `/tmp` и `<data>` лежат на одной ФС: `/dev/sda2`, ext4.
- Последний бэкап — `ummanu-backup-{core,full}-20261002T114729Z.tar` (2,9 ГиБ и 1,0 ГиБ), создан успешно.

**Вердикт: опровергнуто для этого хоста.** Гипотеза остаётся для хостов с tmpfs-`/tmp`.

### 12.14 Масштаб INEF-01, -04, -05, -07, -11, -12, -13

**Что проверено:**
- `production-state.json` и `dispatcher/cleanup.json`;
- `<data>/webproto/runs`;
- `requests` с `product_run.*`;
- размеры журналов голов;
- интервалы тиков по journald (`OnUnitActiveSec=60s`, поэтому интервал ≈ длительность + 60 с);
- `tick_telemetry`;
- `ps` (накопленное CPU);
- `pg_stat_database.sessions` (два замера);
- inotify-наблюдение за `dispatcher/` в течение 300 с;
- 7 одиночных GET.

**Найдено (Ф):**
- **INEF-01.** Сейчас в диспетчере 3 активные записи `records`, 129 `attempts` и 38 `resume_workspaces`. Главный множитель — не число записей, а размер журнала cleanup:
  - `dispatcher/cleanup.json` весит 37,5 МБ: 240 intents, медиана 41 КБ, максимум 675 КБ, основной объём — поля `heads` и `record`;
  - файл переписывается вместе с `production-state.json` (531 КБ, mtime совпадают до 0,07 с);
  - **inotify 23:27:32–23:32:33 (300 с; один тик закончился в 23:29:54, следующий шёл):** `cleanup.json` атомарно заменён (`IN_MOVED_TO`) 91 раз, сериями примерно раз в 0,8 с (t = 145–157 с и 181–219 с). Это ≈3,4 ГБ записи за 5 минут, ≈11 МБ/с в среднем. За то же окно `production-state.json` заменён 12 раз, `resource_health.json` — 1 раз;
  - процесс тика загружен CPU: `production-tick` за 109 с жизни потребил 78 с CPU (72 %), а на хосте всего 3 ядра;
  - длительность тика:

    | Период | Медиана | p90 |
    |---|---|---|
    | 02–04.10 | ≈5 с | 45–55 с |
    | 05.10 | ≈100 с | ≈380 с |

    `tick_telemetry.last.duration_ms` равно 466 308, из них checkpoint — 51–54 с.
  - Причину роста 05.10 без профилирования не разделить (**Г**).
- **INEF-04.** Product-run записей 4: `<data>/webproto/runs/*.json` от 2026-09-06. Аудит `product_run.started` и `.finished` — по 10 строк, все 06.09, роста нет.
  - Карточка по логу: `GET /tasks/{ref}` p50 163 мс, p90 2,9 с (n = 160); замеры 0,17 и 0,14 с.
  - Сама находка (Ф) верна, но на нынешних объёмах цена ничтожна.
- **INEF-05.**
  - Журналов голов 838 (`heads/`, 49 МиБ) плюс 305 в `po-heads` (0,5 МиБ). `du`: `heads` 66 МБ, `po-runs` 28 МБ. Ротации и удаления нет: все run-директории с 24.09 на месте.
  - Самый большой журнал — 1,8 МиБ.
  - Рост за активное время головы (n = 338): p50 49 КиБ/ч, p90 0,48 МиБ/ч, максимум 1,14 МиБ/ч. Оценка аудита ≈1,4 МБ/ч соответствует худшему случаю.
  - Вывод PTY в журнал не пишется: в нём только события с счётчиками байт, хвост лежит в `output.tail`.
- **INEF-07.** `production-state.json` (531 КБ) заменён 12 раз за 300 с, при 3 активных записях. На 1,5 тика это ≈8 перезаписей за тик, ≈6 МБ. Привязать отдельные записи к `wait_vitality` и `review_verdict` без трассировки нельзя.
- **INEF-11.**
  - Страницы: `/` 53,8 КБ (13,7 КБ gzip), `/projects/ummanu` 47,3 КБ (11,8 КБ), `/tasks/ummanu-90` 67,5 КБ (18,2 КБ).
  - В Caddyfile нет `encode`.
  - Автообновление 30 с включено по умолчанию (`localStorage 'ummanu.web.refresh' !== 'off'`). Открытый дашборд даёт ≈155 МБ в сутки, с gzip было бы ≈39 МБ.
  - Латентность по логу с 03.10: `/` p50 1,9 с, p90 4,5 с, максимум 12,9 с (n = 60); `/api/system` p50 1,9 с; `/sprints` p50 0,6 с, p90 3,1 с; `/po/sessions/{id}` p50 0,47 с, p90 2,9 с (n = 1 569).
- **INEF-12.** `pg_stat_database.sessions` = 43 803 при старте postmaster 2026-10-02 08:37 (`stats_reset` NULL). Это ≈505 сессий в час, если считать от старта; средняя сессия ≈36 с. Сейчас открыто 4 соединения `ummanu_app`. Второй замер — 43 832 в 23:31:47: +29 сессий за 5 мин 08 с, то есть ≈340 в час сейчас. Разделить сессии по процессам нельзя: `application_name` пуст.
- **INEF-13.** Supervisor каждой головы держит 0,09–0,17 % CPU, `web-serve` — 3 %, `po-serve` — 0,9 %. Опрос `_await_*` идёт внутри процесса тика, и его долю в 72 % CPU тика без профилировщика не выделить (**Г**).

**Вердикт:**
- INEF-01 — **подтверждено и масштабнее оценки**: дорог не `reader.show`, а 37,5-мегабайтный `cleanup.json`, который переписывается до раза в секунду.
- INEF-04 — **подтверждено, но масштаб ничтожен**.
- INEF-05 — **подтверждено** (нет ротации), рост ниже оценки.
- INEF-07 — **подтверждено** (≈8 перезаписей state за тик).
- INEF-11 — **подтверждено**.
- INEF-12 — **частично**: ≈340 сессий в час, по процессам не разделено.
- Доли INEF-01/07/13 в CPU тика — **не определено по истории**, эксперимент ниже.

### 12.15 Факты репозитория

**Что проверено:** `gh api repos/vladmesh/ummanu/branches/main/protection`, `…/rules/branches/main`, `…/rulesets`; `instance/adapters/*.yaml`; запуски workflow `e2e-synthetic.yml`.

**Найдено (Ф):**
- **Защиты `main` нет:** protection отвечает 404 «Branch not protected», rules и rulesets — `[]`. Значит, `typecheck` (как и любой другой check) не обязателен (CI-01).
- **`e2e-synthetic.yml`** — workflow без тестов: на `workflow_dispatch` он спит заданное число минут и завершается с заданным `outcome`, его писали для live proof sprint:1469.
  - Ни один из 16 адаптеров инстанса на него не ссылается.
  - Единственный адаптер с `validation.e2e` — `codegen-orchestrator.yaml` (`stand-e2e.yml`, `after_merge`, `mega-noop`).
  - Запусков за всё время 2, оба 2026-09-28.
  - По его собственному заголовку его можно удалять.

### 12.16 Что осталось не определено, и как это закрыть

| Пункт | Эксперимент | Риск для спринтов, карточек и PO |
|---|---|---|
| Доли INEF-01/07/13 в CPU тика (72 %, 100–466 с 05.10) | `py-spy record --pid <tick> --duration 120 --rate 20 --nonblocking` на одном тике; альтернатива — `cProfile` через тот же `ummanu dispatcher production-tick` на стенде-реплике | `--nonblocking` не останавливает процесс, тик удлиняется на ≈1–3 %. Это профилировщик на живом процессе, поэтому нужно разрешение владельца; на стенде риска нет |
| Перезаписи state и cleanup по тикам и вызывающим (INEF-01/07): 300-секундного окна мало | inotify на `dispatcher/` (только чтение) на 1 ч, сопоставить с границами тиков в journald; вызывающих показывает py-spy из строки выше | Нулевой: процессы не трогаются |
| Темп `sessions` по процессам (INEF-12) | Включить `log_connections` или задать `application_name` в DSN | Включение `log_connections` требует reload Postgres: доска не прерывается, но это запись в конфиг БД, поэтому только окно владельца. Через DSN — правка кода |
| Узкая гонка BUG-11 | Unit-тест: Codex-TUI профиль, `head.pid` прошлой инкарнации с тем же `run_id` и мёртвым pid, `start` возвращает до записи новой identity | Нулевой, локальный тест |

Временные файлы были в `/tmp/research-ummanu-90` и удалены; фоновых процессов не осталось.


### 12.17 Эксперименты (2026-10-05): доля INEF-01/07/13 в CPU тика диспетчера

**Постановка.**
- Процесс `ummanu dispatcher production-tick` (`ummanu-dispatcher-production.service`, timer `OnUnitActiveSec=60s`).
- `ptrace_scope = 1`, поэтому attach шёл через `sudo -n`.
- Профилировщик `py-spy record --pid <tick> --duration 10 --rate 50 --nonblocking --idle --format raw`. Сэмплы берутся и в ожидании, то есть доля считается от **стенного** времени.
- Параллельно читались `utime/stime/cutime/cstime` из `/proc/<pid>/stat`, а также `journalctl` юнита (systemd-строки `Consumed …` и JSON-итог тика).

**Шаги.**
1. 23:46:41: окно 10 с на хвосте тика pid 3850247 (возраст 340 с).
2. 23:47:07: окно 10 с на первых секундах тика pid 3860324.
3. 23:47:42–23:56:58: 19 окон по 10 с через каждые 30 с на том же тике pid 3860324 (возраст 38…585 с).
4. Итого 21 окно, 210 с под профилировщиком, 10 136 сэмплов. Фазы определялись по кадру под `_production_tick_work` (`dispatch/production.py`), агрегация шла скриптом по `raw`-стекам.

**Наблюдение (Ф).**

*Тики идут без паузы.* Systemd пишет `Starting` в ту же секунду, что и `Finished`: 23:35:30, 23:41:01, 23:47:03, 23:57:33. Тик длится дольше 60 с, поэтому диспетчер занимает ядро непрерывно.

| Тик (старт) | Длительность | CPU юнита (`Consumed`) |
|---|---|---|
| 23:10:39 | 408 с | 4 мин 22 с |
| 23:17:27 | 469 с | 5 мин 43 с |
| 23:25:16 | 278 с | 3 мин 29 с |
| 23:29:54 | 336 с | 3 мин 46 с |
| 23:35:30 | 331 с | 3 мин 55 с |
| 23:41:01 | 362 с | 4 мин 15 с |
| **23:47:03 (профилирован)** | **630 с** | **8 мин 34 с** |

Пик памяти юнита — 3,5 ГБ, плюс 777 МБ swap.

*Фазы тика 23:47:03 (630 с), по окнам:*

| Фаза (`production.py`) | Окна (возраст, с) | Оценка стенного времени | Что внутри |
|---|---|---|---|
| `:427` cleanup `replay` | 4–48 | ≈55 с (9 %) | `cleanup.py:992/1072`; ⅓ — `_save`, ¼ — `read` `cleanup.json` |
| `:453` `_advance_active` | 68–400 | ≈350 с (55 %) | см. ниже |
| `:463` `reconcile_after_merge` | 400–565 | ≈165 с (26 %) | `e2e_after_merge.py:453` `_recover_pending` → `reader.show` → `tasks.py:668` `project_card_by_reference` → `getAllTasks` (`sql_cards.py:876-878`); 75 % сэмплов — ожидание psycopg и `fetchall` |
| `:479` `_production_claim_ready` | ≈585 | ≈5–15 с | `claim.py:122` → `head_health.py:133` probe-подпроцесс (`select`) |
| `:495` checkpoint | 571–630 | 59 с (`duration_ms` 59 026) | `checkpoint.py:570` `_regenerate`/`export_board`. На хвосте прошлого тика (окно 1) 100 % сэмплов приходилось на `_scan_cut` (`checkpoint.py:1359`) → `redact` (`runtime/redact.py:118`): прогон regex `PATTERNS` по каждому файлу среза |

*Внутри `_advance_active` (11 окон, 5 465 сэмплов):*
- **3 140 (57 %)** — одна цепочка: `runtime.py:405/488` `poll_codex_provider_ingress` → `codex_provider_events.py:239` `poll` → `:306` `_advance_cursor` → `:321` `_replace_source` → `runtime.py:352` `persist` → `runtime.py:999` `save_records` → `cleanup.py:396` `remember`.
- Внутри неё `remember` на **каждой** строке rollout без событий делает:
  - полное `json.loads` `cleanup.json` (`cleanup.py:203`, 1 576 сэмплов);
  - полную перезапись с `_compact_heads` (`cleanup.py:233-238`, 1 018 сэмплов);
  - затем `production_state.save`.
- `save_records` при этом проходит по всем записям с workspace (`runtime.py:996-998`).
- Ещё 2 044 сэмпла всего профиля (22 %) — усечённые глубокие стеки: в 1 934 из них только `json/encoder|decoder`. По размеру (38 МБ против 0,53 МБ state) это та же сериализация `cleanup.json` (**О**).
- CPU процесса за возраст 38→400 с: (utime + stime) +323 с за 362 с стенного времени, то есть **89 % ядра**. Из них stime 20 %: запись и fsync файла 38 МБ.

*Сводно по всем 9 185 сэмплам тика 3860324:*

| Кто | Сэмплов | Доля стенного времени |
|---|---|---|
| `cleanup.json` `read/save/remember` (полные стеки) | 3 954 | 43,0 % |
| + усечённые стеки json-сериализации (**О**: та же) | 1 934 | до 21 % |
| `poll_codex_provider_ingress` (включает большую часть строки 1) | 3 394 | 37,0 % |
| `getAllTasks` через `reader.show` в `_recover_pending` | 1 537 | 16,7 % |
| ожидание psycopg | 1 585 | 17,3 % |
| `review_verdict` | 366 | 4,0 % |
| `vitality` | 272 | 3,0 % |
| `_await_*` (опрос INEF-13) | **0** | 0 % |

`cleanup.json` на 23:58 — 37 971 903 байт (в §12.14 было 37,5 МБ).

**Вердикт.**
- **INEF-01 — подтверждено, главный потребитель тика.** Дорог не `reader.show` в `save_records`, а полное чтение и перезапись 38-мегабайтного `cleanup.json` в `CleanupJournal.remember`. Это 43–64 % стенного времени тика, при CPU ≈89 % ядра в этой фазе.
- **INEF-07 — подтверждено, найден вызывающий.** Перезаписи state и `cleanup.json` в серии ≈1/0,8 с (inotify §12.14) порождает телеметрия курсора Codex provider-ingress (`codex_provider_events.py:306` → `persist` → `save_records`): одна полная запись на каждую строку rollout. Сам код помечает её «best effort» (`:323`). `wait_vitality` и `review_verdict` вместе дают ≤7 %.
- **INEF-13 — опровергнуто для тика.** Опрос `_await_*` в 9 185 сэмплах не встретился ни разу. Ожидания в тике — psycopg (17 %) и probe-подпроцессы (≈6 %).
- **Новое (Ф, дополнение к INEF-01).** `e2e_after_merge._recover_pending` (`e2e_after_merge.py:453`) зовёт `reader.show` на каждую карточку очереди. Для архивного дубля ссылки `project_card_by_reference` (`tasks.py:668`) читает `getAllTasks` всего проекта. На этом тике это ≈165 с (26 %).
- **Checkpoint.** 46–59 с на тик, горячая точка — `redact` по всему срезу (`checkpoint.py:1359`, `redact.py:118`).

**Побочные эффекты.** Профилированный тик оказался самым длинным в окне: 630 с и 8 мин 34 с CPU против 278–469 с и 3,5–5,7 мин у шести предыдущих.
- `--nonblocking` процесс не останавливает. Сэмплы, однако, показывают, что лишнее время ушло в цикл provider-ingress, а в 23:47:03 тот же тик запустил Codex-ревьюера `codegen-orchestrator-1531` (`codex-terra-high`, `review-started`). Поэтому рост объясняется нагрузкой (**О**), но вклад профилировщика полностью исключить нельзя (**Г**).
- Тик завершился штатно: `status: ok`, `errors: []`, checkpoint `committed`. Следующий тик стартовал в 23:57:33 без профилировщика.
- Других эффектов не замечено.

### 12.18 Пропущенные эксперименты (infra-карточка ummanu-91)

| Пункт | Причина |
|---|---|
| BUG-11 (убить голову curator/steward) | В `ummanu-90` вердикт **опровергнуто по истории**, а не «не определено»: 2 из 2 реальных Codex-TUI повторных подъёмов на том же `run_id` прошли. Узкая гонка закрывается unit-тестом (§12.16), убивать прод-голову для неё не нужно. |
| DOC-06 | В `ummanu-90` **подтверждено** (9 TUI-сессий curator, каталог `## Skills` без `curate`). |
| INEF-01/04/13, часть «≈20 GET и строки аудита на рендер» | INEF-04 и INEF-11 закрыты в §12.14: латентность по access-логу (n = 160 и 1 569), `product_run.*` — 10+10 строк, роста нет. Открытой осталась только доля в CPU тика. |
| BUG-01 (smuggling) | Пункта нет среди 15 пунктов `ummanu-90`, поэтому он не мог быть оставлен «не определено по истории». По правилу карточки не запускался. Если нужен, его стоит выносить отдельной карточкой. |
