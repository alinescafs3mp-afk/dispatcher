# Sol Link Nightshift

Локальный аварийный оркестратор для продолжения разработки Friday после внезапного исчерпания лимитов у Sol и SolGoodman.

Nightshift использует уже авторизованные подписочные CLI, а не API-ключи:

- `grok-build` или `grok`: Grok 4.6, временный главный архитектор и ревьюер;
- `codex-solgoodman`: GPT-5.6 Luna, основной инженер-исполнитель;
- `codex`: GPT-5.3 Codex Spark, быстрый исполнитель микрозадач.

> Важная проводка аккаунтов: локальный `codex` продолжает линию **SolGoodman** и после исчерпания обычного лимита используется как Spark. Локальный `codex-solgoodman` продолжает линию **Sol** и используется как Luna. Nightshift не угадывает личность предшественника по имени команды и не смешивает их сохранённые сессии.

## Что уже умеет

- поднимает локальную веб-панель на FastAPI без Node.js и сборщика фронтенда;
- показывает состояние трёх агентов, их текущую задачу, модель, reasoning и консольные логи;
- читает окна лимитов Codex через локальный `codex app-server` и показывает их полосами, включая отдельные buckets наподобие `gpt-reserve`;
- читает подписочные кредиты Grok через локальный ACP billing extension (`x.ai/billing`, с резервной поддержкой прежнего `_x.ai/billing`);
- позволяет менять reasoning всех трёх моделей, настройка применяется со следующего обращения;
- даёт отдельный постоянный чат с Grok-архитектором;
- при старте миссии проводит Phase Zero: исследует Git, грязные worktree, ветки, stash, backlog, handoff-файлы, Sol Link state и подходящие локальные Codex-сессии;
- безопасно спасает незакоммиченные изменения исходного checkout в отдельную integration-ветку, не трогая оригинал;
- запускает замкнутый цикл `Grok contract -> Luna/Spark -> validation -> Grok review -> integrate/revise/escalate`;
- держит architect, integration и worker worktree отдельно;
- не передаёт моделям весь чат и все diff повторно: Sol Link хранит компактные task packets, SHA, результаты проверок и артефакты;
- сохраняет миссии, события, логи, usage и reasoning в SQLite;
- после перезапуска отмечает незавершённую миссию как paused и повторно исследует сохранившиеся ветки/worktree перед продолжением;
- требует ручного решения для high/critical risk и может быть настроен строже;
- отбрасывает правки в защищённых путях и файлы с credential-shaped содержимым до коммита в worker-ветку.

Полная директива поведения аварийной команды лежит в корне: [`EMERGENCY_TAKEOVER_DIRECTIVE.md`](EMERGENCY_TAKEOVER_DIRECTIVE.md).

## Быстрый запуск

Требования:

- Linux или macOS с Git;
- Python 3.11+;
- уже залогиненные `grok-build`, `codex` и `codex-solgoodman`;
- целевой проект должен быть Git-репозиторием.

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

Панель по умолчанию откроется на `http://127.0.0.1:8787`.

Альтернативный установщик:

```bash
./scripts/install.sh /absolute/path/to/friday
source .venv/bin/activate
nightshift serve
```

## Первый боевой запуск

1. Выполнить `nightshift doctor`. У всех трёх lanes должно быть `ready: true`.
2. Выполнить `nightshift quotas`. Для Codex должны появиться реальные server-side windows, для Grok кредитный процент, если текущий тариф его отдаёт.
3. При необходимости поправить `nightshift.toml`: repo, validation commands, protected/high-risk paths.
4. Запустить `nightshift serve`.
5. В поле Mission goal оставить стандартную цель или описать остаток текущей работы.
6. Нажать **Start emergency takeover**.
7. Следить за recovery dossier, task ledger и консолями. High-risk integration не пройдёт без Human gate.

Nightshift не пушит и не сливает изменения в `main`. Принятые задачи собираются в отдельной ветке:

```text
nightshift/<mission-id>/integration
```

Её можно проверить обычным Git-инструментарием и слить вручную после возвращения Sol.

## Команды

```bash
nightshift init --repo /path/to/friday       # создать nightshift.toml
nightshift doctor                            # проверить Git, CLI и подписочные логины
nightshift quotas                            # прочитать лимиты/кредиты
nightshift scan                              # только Phase Zero, без моделей и правок
nightshift serve                             # веб-пульт
nightshift directive ./DIRECTIVE.md          # скопировать аварийную директиву
```

Для другого файла конфигурации:

```bash
nightshift --config ~/.config/nightshift/friday.toml serve
```

## Конфигурация

Сгенерированный `nightshift.toml` уже содержит нужную схему. Репозиторий также включает [`nightshift.example.toml`](nightshift.example.toml).

Пример полезных проектных проверок:

```toml
[project]
repo = "/home/jericho/friday"
validation_commands = [
  "python -m pytest -q",
  "python -m ruff check .",
]
```

Команды validation запускаются **без shell**. Конструкции `&&`, `|`, `;`, redirects, `sudo`, package managers и абсолютные пути к произвольным бинарникам отклоняются. Это намеренно: задача проверки не должна превращаться во второй автономный агент.

### Reasoning

В карточке каждого агента есть picker reasoning. Nightshift передаёт:

- Grok: `--effort <value>`;
- Codex: `-c model_reasoning_effort="<value>"`.

Для Codex меню обновляется из `model/list`, если текущий CLI публикует capabilities. Выбранное значение сохраняется и применяется со следующего model turn. Уже выполняющийся процесс не перенастраивается на лету.

### Полный доступ

По умолчанию:

- Grok-архитектор работает в режиме `--sandbox read-only` с отключёнными edit/write tools;
- Luna и Spark работают в Codex `workspace-write` sandbox;
- исходный checkout не является их рабочим каталогом;
- API-key и credential-shaped переменные удаляются из окружения дочерних процессов.

`unsafe_full_access = true` включает Codex bypass sandbox для соответствующего worker lane. Это аварийный рубильник, а не ускоритель. Для Friday его стоит оставлять выключенным.

## Как Nightshift подхватывает внезапно оборванную работу

На старте создаётся dossier с:

- HEAD, branch, index, unstaged/untracked state и stash;
- всеми worktree и их грязными diff;
- локальными ветками по времени активности;
- backlog/roadmap/TODO/handoff/`outer_sol` материалами;
- Sol Link watcher state;
- кандидатами последних сессий обоих Codex-аккаунтов;
- безопасными копиями rescue patch и незакоммиченного кода.

Затем Spark получает read-only resume именно линии SolGoodman, а Luna линии Sol. Их задача на этом шаге не писать код, а выдать короткий handoff: что делалось, что изменено, что не проверено, где остановились. Grok сверяет эти рассказы с Git и тестами, а не принимает их за истину.

При рестарте Nightshift не пытается воскресить уже умерший OS-процесс worker. Он сохраняет SQLite, integration branch и worker branches/worktrees, повторяет Phase Zero, сверяет базу с Git и только потом выдаёт новую работу. Это медленнее магии, зато не плодит фантомные коммиты.

## Маршрутизация задач

Grok выбирает одного исполнителя на пакет.

**Spark:** обычно 1-3 файла, низкий риск, однозначное изменение, точные проверки, механический refactor, unit test, typing/lint fix.

**Luna:** расследование бага, несколько взаимодействующих модулей, интеграционные тесты, продолжение частичной реализации, сложная доводка Spark, medium risk.

Security boundaries, irreversible migrations, privileges, sandbox, auth, `engineer_mode`, destructive data changes и массовые удаления автоматически поднимают риск и требуют человека.

## Лимиты и токены

Codex quota refresh не обращается к модели. Nightshift открывает локальный app-server, вызывает `account/rateLimits/read` и закрывает процесс. Grok billing refresh аналогично использует ACP extension и локальный cached login.

Токены расходуются только на реальные model turns:

- forensic handoff из подходящей predecessor-сессии;
- решение Grok;
- реализация worker;
- review Grok;
- отдельный operator chat.

Полный diff не вклеивается в prompt. Grok получает base/head SHA и читает его из общего Git object store. В SQLite сохраняется usage, полученный из CLI events, чтобы токеновая гидравлика была видна, а не оценивалась по звуку труб.

## Безопасность и ограничения

Прочитать [`SECURITY.md`](SECURITY.md) перед запуском на недоверенном репозитории.

Ключевые ограничения:

- это локальный orchestration tool, а не security boundary уровня VM;
- sandbox конкретного CLI и отдельные worktree сильно уменьшают blast radius, но не доказывают отсутствие всех способов чтения файлов или сети;
- model-authored project tests могут исполнять код целевого репозитория;
- для hostile repository используйте отдельного Unix-пользователя, контейнер или VM без домашних секретов;
- живой login/limit handshake с вашими локальными CLI невозможно проверить в CI этого репозитория, поэтому есть `nightshift doctor`, а протоколы покрыты fake-CLI integration tests.

## Разработка

```bash
python -m pip install -e ".[dev]"
python -m compileall -q nightshift tests
node --check nightshift/static/app.js
pytest
ruff check .
python -m build
```

Архитектура описана в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Происхождение

В приложенный ранний `dispatcher-main.zip` уже были хорошие идеи: PySide6-пульт, CLI adapters, durable state, worktree isolation, redaction и test scaffolding. Nightshift сохраняет эти несущие конструкции, но заменяет прежнюю большую multi-agent схему на узкий аварийный контур из трёх lanes и браузерный интерфейс без тяжёлого desktop runtime.

## Лицензия

MIT, см. [`LICENSE`](LICENSE).
