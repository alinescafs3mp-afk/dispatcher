# Sol Link Dispatcher

Локальный подписочный оркестратор для непрерывной разработки Friday. Он управляет двумя режимами одной команды через уже авторизованные CLI, без API-ключей и без смешивания аккаунтов, сессий и ролей.

## Профили

В веб-панели есть переключатель **Бой / Резерв**. Профиль нельзя менять, пока жив текущий mission task, включая ручную паузу, или выполняется model turn. Сохранённую после рестарта paused-миссию можно оставить в истории: при её возобновлении Dispatcher сам восстановит записанный профиль и состав команды.

### Бой

Штатный контур разработки:

- `codex` занимает место **Sol**: lead architect, владелец архитектуры и бэклога, ревьюер и интеграционная инстанция;
- `codex-solgoodman` занимает место **SolGoodman**: основной инженер, отладчик и владелец реализации;
- `grok-build` или `grok` подключается опционально как быстрый помощник с другим углом обзора.

Оба Codex-участника по умолчанию работают с `model_reasoning_effort="ultra"`. Конкретная модель оставлена за уже настроенными wrapper/profile командами `codex` и `codex-solgoodman`, поэтому Dispatcher не ломает локальный выбор Sol жёстким `--model`. Для автоматических боевых ходов обоим включён `--dangerously-bypass-approvals-and-sandbox`; опциональный Grok получает `--always-approve --sandbox off`.

Sol сверяет состояние репозитория, формирует, дополняет и декомпозирует бэклог, затем выдаёт по одному ограниченному task packet. SolGoodman реализует основную работу. Подключённый Grok получает только подходящие вспомогательные пакеты. Каждая реализация проходит детерминированную проверку scope, validation, ревью архитектора и, при необходимости, human gate.

### Резерв

Аварийный контур, существовавший до появления профилей:

- Grok 4.6 временно становится архитектором и ревьюером;
- `codex-solgoodman` используется как **Codex Luna**, основной аварийный исполнитель;
- `codex` используется как **Codex Spark**, быстрый исполнитель микрозадач.

Luna по умолчанию работает на самом сильном поддерживаемом reasoning `max`. Для автоматических ходов Grok, Luna и Spark получают полный доступ: Grok запускается с `--always-approve --sandbox off`, а оба Codex-процесса с `--dangerously-bypass-approvals-and-sandbox`. Резервный профиль проводит Phase Zero, восстанавливает оборванную работу, сверяет Git, локальные сессии и handoff-материалы, после чего допинывает безопасно восстановимый остаток.

При чтении лимитов аварийная Luna объявляет Codex app-server поддержку Luna Reserve. Backend раскрывает отдельный `gpt-reserve` bucket только после фактической блокировки обычного included usage и только для подходящего ChatGPT-аккаунта; значение 99% само по себе ещё не обязано показывать этот bucket. Когда backend возвращает авторизующий `luna_reserve` banner или подтверждённое Reserve-окно, worker turn, predecessor recovery и direct chat автоматически идут через модель-маршрут `gpt-reserve`. После исчерпания Reserve применяется обычный межаккаунтный fallback на Spark для достаточно узких задач.

**Luna Reserve и профиль `reserve` — разные уровни.** Профиль Dispatcher состоит из Grok, Luna и Spark, но провайдерский Reserve относится только к GPT-5.6 Luna. `GPT-5.3-Codex-Spark` / `codex_bengalfox` имеет собственные обычные 5-часовую и недельную корзины; это не второй Reserve. Banked/reset credits, если backend их возвращает, также являются отдельным механизмом и показываются в диагностике независимо.

Старые Codex app-server принимают для `account/rateLimits/read` только `params: null`. Dispatcher сначала пробует capability-объект и при точном legacy-ответе безопасно повторяет запрос с `null`, поэтому обычные полоски не исчезают. В таком случае `raw.luna_reserve_status` равен `legacy_app_server`: это означает, что установленный CLI умеет отдать обычные лимиты, но не умеет запросить Luna Reserve. Доступ не выдумывается и `--model gpt-reserve` не включается без backend-авторизации.

> Проводка аккаунтов намеренная: в резерве локальный `codex` продолжает линию SolGoodman и становится Spark, а `codex-solgoodman` продолжает линию Sol и становится Luna. Dispatcher не угадывает владельца сессии по времени и не cross-resume-ит соседний аккаунт.

## Рабочие контуры Friday

Dispatcher больше не предполагает, что вся жизнь Солов происходила строго внутри одного `cwd`. Для текущего развёртывания базовая конфигурация выглядит так:

```toml
[project]
repo = "/jericho/jericho"
operational_roots = ["~/.jericho"]
```

`project.repo` остаётся Git-источником истины и основой integration/architect/worker worktree. `operational_roots` используются как дополнительные поверхности непрерывности: там ищутся подходящие backlog/handoff-файлы, состояние watchers и учитывается `cwd` найденных Codex-сессий. Поэтому сессия Сола из `~/.jericho` получает тот же статус релевантной рабочей сессии, а не выглядит случайной только из-за другого каталога.

Full-access участники могут читать и обслуживать operational root, когда это прямо требуется целью миссии или операторской подсказкой. Однако такой каталог не становится вторым неявным Git-репозиторием: Dispatcher не коммитит его побочные эффекты в integration-ветку. Продуктовые изменения по-прежнему должны проходить через `project.repo` и явный task packet.

## Прямые линии к участникам

Панель позволяет говорить не только с архитектором, но и с любым активным участником профиля.

- **Talk now** открывает отдельный read-only model turn. Он не редактирует код, не создаёт задачу и не вмешивается молча в автоматический цикл.
- **Nudge next work turn** сохраняет операторскую заметку в SQLite и прикладывает её к следующему рабочему ходу выбранного участника.
- **Auto** разговаривает сразу, если lane свободен, либо автоматически превращает сообщение в durable nudge, если lane занят.

Nudge считается доставленным после успешного хода либо после появления подтверждения от провайдера: события или финального ответа. Если CLI падает до любого такого подтверждения, заметка остаётся в очереди и будет предложена повторно. Это позволяет вручную остановить архитектурный занос, уточнить ограничение или попросить участника перепроверить странное поведение, не убивая текущий процесс.

## Realtime-панель

Всё изменяемое состояние обновляется без перезагрузки страницы:

- активный профиль и подключение Grok;
- миссия, пауза, остановка, resume и human gate;
- статусы участников, текущие задачи и ошибки;
- reasoning, режим доступа, usage и лимиты;
- task ledger, логи, чат и статус queued/delivered у nudges.

Основной канал обновления работает через WebSocket: каждое уведомление запускает single-flight загрузку нового authoritative snapshot. Устаревшие HTTP-ответы отбрасываются, а reconnect, пробуждение вкладки и периодический polling запускают дополнительную синхронизацию.

## Быстрый запуск

Требования:

- Linux или macOS с Git;
- Python 3.11+;
- авторизованные `codex`, `codex-solgoodman` и, при необходимости, `grok-build`/`grok`;
- целевой проект является Git-репозиторием.

```bash
git clone https://github.com/alinescafs3mp-afk/dispatcher.git
cd dispatcher
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"

nightshift init --repo /jericho/jericho
nightshift doctor
nightshift quotas
nightshift serve
```

Панель по умолчанию откроется на `http://127.0.0.1:8787`. Loopback-имена
`localhost`, `127.0.0.1` и `::1` принимаются автоматически. При публикации через
reverse proxy или при привязке к другому имени перечислите точные допустимые Host
в `server.allowed_hosts`; wildcard bind без такого списка намеренно не запускается.

```toml
[server]
host = "127.0.0.1"
port = 8787
allowed_hosts = []
```

Альтернативный установщик:

```bash
./scripts/install.sh /jericho/jericho
source .venv/bin/activate
nightshift serve
```

## Первый запуск

1. Выполнить `nightshift doctor` и проверить `ready: true` у обязательных lanes.
2. Выполнить `nightshift quotas` и убедиться, что читаются нужные подписочные окна.
3. Проверить `project.repo`, `project.operational_roots`, validation commands и protected/high-risk paths в `nightshift.toml`.
4. Запустить `nightshift serve`.
5. Выбрать **Бой** или **Резерв**. В бою при необходимости включить Grok.
6. Задать цель миссии и запустить её.
7. Следить за task ledger, логами и direct lines. High/critical risk не интегрируется без решения человека.

Dispatcher не пушит изменения в целевой `main`. Принятые задачи собираются в отдельной integration-ветке:

```text
nightshift/<mission-id>/integration
```

Эту ветку следует проверить и слить обычным Git-процессом.

## Команды

```bash
nightshift init --repo /jericho/jericho
nightshift doctor
nightshift quotas
nightshift scan
nightshift serve
nightshift directive --profile combat ./COMBAT_DIRECTIVE.md
```

Для другого файла конфигурации:

```bash
nightshift --config ~/.config/nightshift/friday.toml serve
```

## Ключевая конфигурация профилей

```toml
[profiles]
default = "reserve"
combat_grok_enabled = false
reserve_grok_full_access = true
reserve_luna_effort = "max"
reserve_luna_full_access = true
reserve_spark_full_access = true

# Пустое имя модели оставляет выбор за локальным wrapper/profile.
combat_sol_model = ""
combat_sol_effort = "ultra"
combat_sol_full_access = true
combat_goodman_model = ""
combat_goodman_effort = "ultra"
combat_goodman_full_access = true

combat_grok_model = "grok-4.6"
combat_grok_effort = "xhigh"
combat_grok_full_access = true
```

Reasoning picker в интерфейсе сохраняется отдельно для каждого профиля и логического lane. Новое значение применяется со следующего обращения к модели.

## Изоляция и полный доступ

Каждая миссия имеет отдельные integration, architect и worker worktree. Исходный checkout не используется как рабочий каталог моделей.

Полный доступ для всех автоматических участников обоих профилей включён намеренно под доверенный Friday. Для Codex это реальный bypass approvals и sandbox; для Grok это `--always-approve --sandbox off`. Отдельные worktree, protected paths, secret scanning, validation и human gate уменьшают blast radius, но не превращают процесс в VM security boundary.

Даже для full-access участников direct chat всегда запускается read-only. Автоматический боевой Sol работает в disposable architect worktree, который жёстко сбрасывается после каждого хода. Долговечные изменения продукта всё равно проходят через worker branch, validation, review и integration.

Для неизвестного или враждебного репозитория отключите full access и запускайте Dispatcher под отдельным Unix-пользователем, в контейнере или VM без домашних секретов. Подробности в [`SECURITY.md`](SECURITY.md).

## Восстановление

Резервный профиль собирает recovery dossier с:

- HEAD, branch, index, unstaged, untracked и stash;
- локальными worktree, ветками и безопасными dirty patches;
- backlog, roadmap, TODO, handoff и `outer_sol` материалами из repo и operational roots;
- состоянием Sol Link watchers, включая `~/.jericho`;
- кандидатами Codex-сессий строго по обнаруженному account home;
- безопасной копией прерванного рабочего дерева в integration-ветке.

После рестарта незавершённая миссия становится paused, открытые задачи помечаются interrupted, затем состояние повторно сверяется с Git перед продолжением. Профиль, подключение Grok, architect session ID и счётчик ротации сохраняются отдельно для каждой миссии, поэтому возобновление не подхватывает контекст соседнего запуска.

## Проверки разработки

```bash
python -m pip install -e ".[dev]"
ruff check nightshift tests
pytest -q
python -m compileall -q nightshift tests
node --check nightshift/static/app.js
python -m build
```

Архитектура описана в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Лицензия MIT.
