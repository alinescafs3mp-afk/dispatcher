# Sol Link Dispatcher

Локальный оркестратор для разработки Friday через уже авторизованные подписочные CLI. Он поддерживает два режима одной команды:

- **Бой / `combat`**: штатная разработка. Sol ведёт архитектуру и живой backlog, SolGoodman реализует и отлаживает, Grok можно подключить как быстрого дополнительного исполнителя с другим углом зрения.
- **Резерв / `reserve`**: аварийное продолжение работы после исчерпания обычных лимитов или обрыва сессий Sol и SolGoodman. Grok временно становится архитектором, Luna берёт крупную реализацию, Spark получает микрозадачи.

Dispatcher не требует API-ключей и не переключает подписочные CLI на metered API billing. Он использует их существующие локальные логины, изолированные Git worktree, SQLite-реестр и браузерный пульт.

## Проводка профилей

| Режим | Роль | Отображаемый участник | Физический CLI |
|---|---|---|---|
| Бой | lead architect, backlog owner, reviewer | **Sol** | `codex` |
| Бой | implementation owner, debugger | **SolGoodman** | `codex-solgoodman` |
| Бой | optional fast assistant | **Grok 4.6** | `grok-build` или `grok` |
| Резерв | temporary architect and reviewer | **Grok 4.6** | `grok-build` или `grok` |
| Резерв | primary implementation worker | **Codex Luna** | `codex-solgoodman` |
| Резерв | bounded micro worker | **Codex Spark** | `codex` |

Внутренний машинный контракт сохраняет три стабильных lane key: `grok`, `luna`, `spark`. Их физическая проводка меняется вместе с профилем, но уже проверенный цикл orchestration остаётся тем же. Сессии, reasoning-настройки, участники, логи и chat-каналы разделены по профилям.

> В резервном профиле названия локальных команд не совпадают с именами предшественников. `codex` продолжает линию SolGoodman и после исчерпания обычного лимита используется как Spark. `codex-solgoodman` продолжает линию Sol и используется как Luna. Dispatcher не смешивает эти сохранённые сессии.

## Что умеет

- переключает **Бой / Резерв** из веб-пульта, но только когда нет активной миссии или модельного хода;
- запоминает выбранный профиль и состояние опционального Grok;
- закрепляет профиль в каждой миссии и автоматически восстанавливает правильную проводку при resume;
- в бою поручает Sol формировать, исправлять, декомпозировать и закрывать backlog;
- в резерве проводит Phase Zero и допинывает подтверждённый остаток работы;
- запускает замкнутый цикл `architect contract -> worker -> deterministic validation -> architect review -> integrate/revise/escalate`;
- держит source checkout, integration worktree, architect worktree и worker worktree раздельно;
- безопасно переносит незакоммиченные изменения исходного checkout в отдельную integration-ветку, не очищая оригинал;
- сохраняет миссии, task packets, события, логи, usage, reasoning, профильные настройки и chat в SQLite WAL;
- после рестарта отмечает незавершённые миссии как paused и сверяет SQLite с фактическим Git;
- измеряет scope и risk до модельного review;
- требует человека для high/critical integration и может быть настроен строже;
- отбрасывает protected paths и credential-shaped содержимое до worker commit;
- читает Codex quota windows через локальный `codex app-server`;
- читает Grok credits через локальный ACP billing extension с fallback-командой;
- показывает состояние всех участников, reasoning, usage, лимиты, task ledger и потоковые логи.

## Прямые линии к участникам

Пульт позволяет выбрать любого активного участника и отправить сообщение одним из трёх способов:

- **Auto**: если lane свободен, открывается read-only диалог; если занят, сообщение ставится в очередь как nudge.
- **Talk now**: немедленный read-only model turn. Для занятого lane запрос отклоняется вместо скрытого ожидания.
- **Nudge next turn**: сообщение долговечно сохраняется и добавляется в следующий модельный ход выбранного участника.

Прямой chat не редактирует файлы, не создаёт task packet и не интегрирует изменения. Это канал наблюдения и ручного управления. Nudge тоже не ломает текущий контракт посередине: он попадает в ближайший следующий turn и остаётся под ограничениями task packet, stop conditions и safety policy.

Для HTTP API можно адресовать участника по отображаемому имени, CLI, agent ID либо явно по lane key через `slot:grok`, `slot:luna`, `slot:spark`. В бою простое имя `Grok` означает опционального Grok-помощника, а `slot:grok` означает внутренний architect lane, то есть Sol.

## Realtime без перезагрузки

Веб-интерфейс получает authoritative snapshot и затем слушает WebSocket. Обновляются:

- активный профиль и доступность Grok;
- миссия и её переходы состояния;
- task packets, attempts, review и human gate;
- статусы моделей и текущие задачи;
- stdout/stderr, assistant output, tool и validation logs;
- адресный chat и состояние queued/delivered nudges;
- token usage и quota windows;
- reasoning picker и обнаруженные model capabilities.

Snapshot содержит последний event sequence. Клиент отслеживает применённый и наблюдаемый sequence, обнаруживает разрывы, делает resync, игнорирует устаревшие HTTP-ответы, держит single-flight refresh, проверяет heartbeat, восстанавливается после сна вкладки и переподключается с backoff и jitter. Десятисекундный polling остаётся страховочной сеткой.

## Быстрый запуск

Требования:

- Linux или macOS с Git;
- Python 3.11+;
- авторизованные `codex`, `codex-solgoodman` и, для Grok lane, `grok-build` или `grok`;
- целевой проект является Git-репозиторием.

```bash
git clone https://github.com/alinescafs3mp-afk/dispatcher.git
cd dispatcher

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"

nightshift init --repo /absolute/path/to/friday
nightshift doctor
nightshift quotas
nightshift serve
```

Пульт по умолчанию открывается на `http://127.0.0.1:8787`.

Альтернативный установщик:

```bash
./scripts/install.sh /absolute/path/to/friday
source .venv/bin/activate
nightshift serve
```

## Первый запуск

1. Откройте `nightshift.toml` и проверьте `project.repo`, validation commands и protected/high-risk paths.
2. Запустите `nightshift serve`.
3. Выберите **Бой** или **Резерв**.
4. В бою при необходимости включите `подключить Grok`.
5. Нажмите **Doctor** и убедитесь, что обязательные lane готовы.
6. Проверьте лимиты.
7. Задайте mission goal и начните миссию.
8. Следите за task ledger, participant logs и human gate.

Переключение режима заблокировано во время активной, paused или awaiting-human миссии. Остановите или завершите её перед сменой профиля. Сохранённая миссия всегда возобновляется со своим исходным профилем.

Dispatcher не пушит результат в `main` целевого проекта. Принятые задачи собираются в отдельной ветке:

```text
nightshift/<mission-id>/integration
```

Её следует проверить и слить обычными Git-инструментами.

## CLI

```bash
nightshift init --repo /path/to/friday
nightshift doctor
nightshift quotas
nightshift scan
nightshift serve
nightshift directive --profile reserve ./EMERGENCY_TAKEOVER_DIRECTIVE.md
nightshift directive --profile combat ./COMBAT_OPERATIONS_DIRECTIVE.md
nightshift --version
```

Для другого файла конфигурации:

```bash
nightshift --config ~/.config/nightshift/friday.toml serve
```

## Конфигурация профилей

Сгенерированный `nightshift.toml` и [`nightshift.example.toml`](nightshift.example.toml) содержат:

```toml
[profiles]
default = "reserve"
combat_grok_enabled = false

# Пустое имя модели оставляет штатный выбор авторизованному wrapper/profile.
combat_sol_model = ""
combat_sol_effort = "max"
combat_goodman_model = ""
combat_goodman_effort = "max"
combat_grok_model = "grok-4.6"
combat_grok_effort = "xhigh"
```

Физические CLI настраиваются в `[agents.grok]`, `[agents.spark]`, `[agents.luna]`. Это шаблоны проводки, а не фиксированные роли во всех режимах.

Полные директивы:

- [`COMBAT_OPERATIONS_DIRECTIVE.md`](COMBAT_OPERATIONS_DIRECTIVE.md)
- [`EMERGENCY_TAKEOVER_DIRECTIVE.md`](EMERGENCY_TAKEOVER_DIRECTIVE.md)

### Validation

```toml
[project]
repo = "/home/jericho/friday"
validation_commands = [
  "python -m pytest -q",
  "python -m ruff check .",
]
```

Validation запускается без shell. Операторы `&&`, `|`, `;`, redirects, command substitution, `sudo`, произвольные абсолютные executable и неразрешённые команды отклоняются.

### Reasoning

Picker сохраняется отдельно для каждого профиля и logical lane. Изменение применяется со следующего model turn.

- Grok CLI получает `--effort <value>`.
- Codex CLI получает `-c model_reasoning_effort="<value>"`.
- Codex options уточняются через `model/list`, когда CLI публикует capabilities.

### Полный доступ

По умолчанию architect работает read-only, workers работают в workspace sandbox, исходный checkout не является их рабочим каталогом, а credential-shaped environment variables удаляются из дочерних процессов.

`unsafe_full_access = true` включает Codex bypass sandbox только для соответствующего writable worker lane. Для Friday это стоит оставлять выключенным.

## Боевой профиль

Sol получает фактический integration HEAD, repository dossier, живой backlog и компактный mission ledger. Он обязан:

- сверить цель человека с кодом, тестами, архитектурой и backlog;
- добавить или декомпозировать отсутствующую работу через bounded task packets;
- выбрать ровно одну implementation-ready задачу;
- отправить сложную работу SolGoodman;
- использовать Grok только для подходящих ограниченных задач, когда helper включён;
- проверить реальный Git diff и validation evidence;
- провести отдельный финальный аудит перед `done`.

Sol остаётся read-only architect. Изменение backlog-файла тоже является реализацией и проходит через worker packet и review. Это не ограничение интеллекта, а способ не смешивать решение, исполнение и подтверждение в одном непрозрачном ходе.

## Резервный профиль

Резерв начинает с dossier:

- HEAD, branch, index, unstaged/untracked state и stash;
- worktree и их безопасные dirty patch;
- локальные ветки по активности;
- backlog, roadmap, TODO, handoff и `outer_sol` материалы;
- Sol Link watcher state;
- кандидаты последних сессий обоих Codex-аккаунтов;
- безопасная rescue-копия незакоммиченного кода.

Spark получает read-only resume линии SolGoodman, Luna получает линию Sol. Они формируют handoff, а Grok сверяет его с Git и тестами. После рестарта мёртвый OS-процесс не «воскрешается»: код восстанавливается из Git, conversation session используется только как дополнительное свидетельство.

## Безопасность

Перед запуском на недоверенном репозитории прочитайте [`SECURITY.md`](SECURITY.md).

Ключевые ограничения:

- это локальный orchestration tool, не VM-level security boundary;
- repository-controlled tests исполняют код;
- live subscription login невозможно полноценно проверить в публичном CI;
- для hostile repository нужен отдельный Unix user, container или VM без домашних секретов;
- dashboard не имеет собственной аутентификации и должен оставаться на loopback либо находиться за authenticated TLS proxy;
- human gate и secret scanner снижают риск, но не доказывают отсутствие любой уязвимости.

## Разработка

```bash
python -m pip install -e ".[dev]"
ruff check nightshift tests
pytest -q
python -m compileall -q nightshift tests
node --check nightshift/static/app.js
python -m build
```

Архитектура описана в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Лицензия

MIT, см. [`LICENSE`](LICENSE).
