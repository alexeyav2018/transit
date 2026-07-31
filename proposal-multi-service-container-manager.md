# Предложение: доработка `opentest-testlib` для поддержки нескольких SUT-контейнеров

> **Автор:** sf-service-strategy-rules-test (cross-service потребитель)
> **Дата:** 2026-08-01
> **Целевая библиотека:** `io.opentest:opentest-testlib:1.0.0-SNAPSHOT` (локальный путь `~/projects/opentest-testlib-develop`)
> **Потребитель-мотиватор:** `sf-service-strategy-rules` — у нас **3 SUT-сервиса** (ctrl/distr/simulation), а библиотечный механизм подъёма SUT-контейнеров умеет поднимать только **один**.

---

## 1. Мотивация (ЗАЧЕМ)

### 1.1. Текущее состояние в библиотеке

Библиотечный компонент, отвечающий за подъём SUT-контейнера (далее — `ContainerManager`; исходник: `core/src/main/java/io/opentest/bdd/ContainerManager.java`), управляет **ровно одним** контейнером через Testcontainers:

- Создаёт ровно один `GenericContainer`, конфигурирует `withNetworkMode("host")`, запускает его, делает health-check.
- В `@PreDestroy` гасит контейнер + перехваченные процессы `podman logs -f`.
- Конфигурация читается из единственного блока `test.service.*` (singular): `image`, `port`, `enabled`, `startupTimeoutSeconds`, `healthPath`, env.

YAML-схема (из `smoke/src/test/resources/application-test.yml`):

```yaml
test:
  service:                     # ← SINGULAR: ровно один сервис
    enabled: true
    image: localhost/<service>:latest
    port: 8080
    startup-timeout-seconds: 120
    health-path: /actuator/health
    env:
      SPRING_PROFILES_ACTIVE: local
```

### 1.2. Проблема в нашем проекте

`sf-service-strategy-rules` — микросервисная система из 3-х бизнес-сервисов. Cross-service тесты должны поднимать **все три** одновременно:

| SUT | Порт | Образ (из `podman-manage.yaml`) |
|-----|------|--------------------------------|
| `ctrl`       | 8085 | `strategy-rules-ctrl:latest`       |
| `distr`      | 8086 | `strategy-rules-distr:latest`      |
| `simulation` | 8087 | `strategy-rules-simulation:latest` |

Все три должны подниматься на host-сети, стартовать параллельно, проходить health-check, шатдауниться вместе с контекстом. Тесты шагают по ним кросс-сервисно: запрос в `distr` → событие в Kafka → обработка в `simulation` → строка в БД.

### 1.3. Текущее обходное решение в потребителе

Потребитель вынужден **писать собственный класс-обёртку**, который:

- Копирует логику библиотечного `ContainerManager` (подъём контейнера, host-network, health-check).
- Разворачивает её на N контейнеров.
- Вводит собственную YAML-схему с Map-структурой (отдельную от библиотечной singular-схемы).

Работает, но нарушает DRY: библиотека умеет "один SUT-контейнер", потребитель дублирует ради "N SUT-контейнеров". При любом улучшении в библиотеке (метрики, трейсинг, продвинутый log-capture) — все потребители отстают.

### 1.4. Цель

Переработать библиотечный механизм подъёма SUT-контейнеров так, чтобы он **из коробки** управлял **N контейнерами** (1 или больше) с единой YAML-схемой. Singular-схема заменяется на plural-схему. Это **breaking change** в публичной YAML-конфигурации — допускается по согласованию с заказчиком.

---

## 2. Системные требования (ЧТО МЕНЯЕТСЯ)

### 2.1. Что должно работать

**R-1. Поддержка N контейнеров через единый механизм.** Потребитель описывает карту сервисов в YAML; библиотечный `ContainerManager` поднимает их все в `@PostConstruct`, гасит в `@PreDestroy`, делает health-check для каждого.

**R-2. Параллельный health-check.** Все контейнеры проверяются на готовность **параллельно**, суммарное время ожидания ≈ `max(startupTimeout)` вместо суммы.

**R-3. Log-capture для каждого контейнера.** Каждый SUT-контейнер получает свой файл `logs/<name>-<date>.log` (а не общий `service-<date>.log`). Префикс берётся из имени сервиса.

**R-4. Программный доступ к списку живых контейнеров.** Step-definitions могут получить `baseUrl` конкретного сервиса по имени (для HTTP-шагов, проверки состояния и т.п.).

**R-5. Полное отключение через `enabled: false`.** Если для конкретного сервиса `enabled: false` — он не поднимается (считается, что уже работает снаружи), health-check пропускается.

**R-6. Полное отключение для всего механизма.** Если ни один сервис не описан (или все `enabled: false`) — `ContainerManager` ничего не делает (как сейчас при singular `enabled: false`).

### 2.2. Что **намеренно ломается** (breaking changes)

**B-1. Singular-схема `test.service` удаляется.** Существующие потребители, использующие `test.service.*`, **должны мигрировать** на `test.services.*`. Это упрощает кодовую базу библиотеки, но требует однократной миграции в каждом потребительском проекте.

**B-2. Поле `name` в `ServiceConfig` становится обязательным.** Раньше имя контейнера дефолтовалось ключом карты — теперь оно должно быть указано явно. Разделяет логическое имя (ключ карты для шагов и API) и физическое имя контейнера в podman/docker.

### 2.3. Чего делать **не нужно**

- **Не нужно** тащить инфру (postgres/kafka/zookeeper) в библиотеку — это ответственность потребителя.
- **Не нужно** менять `MockServerManager` / мок-схему — это другой механизм.
- **Не нужно** делать `start()` / `stop()` per-scenario — это single-shot на прогон.
- **Не нужно** поддерживать singular как deprecated-алиас — обратная совместимость не требуется (B-1).

---

## 3. Что должно получиться (без деталей реализации)

### 3.1. Новая YAML-схема

Единственная допустимая схема — `test.services` (plural, `Map<String, ServiceConfig>`). Старая singular-схема **не поддерживается** (B-1).

Концептуальная структура YAML для нашего потребителя:

```yaml
test:
  services:                       # ← ЕДИНСТВЕННАЯ схема
    ctrl:                         # ключ — логическое имя (для шагов и API)
      enabled: true
      name: ctrl                  # имя контейнера в podman/docker (обязательно, B-2)
      image: strategy-rules-ctrl:latest
      port: 8085
      startup-timeout-seconds: 180
      health-path: /actuator/health
      env:
        SPRING_PROFILES_ACTIVE: local
        DISTR_URL: http://localhost:8086   # cross-service env
    distr:
      enabled: true
      name: distr
      image: strategy-rules-distr:latest
      port: 8086
      ...
    simulation:
      enabled: true
      name: simulation
      image: strategy-rules-simulation:latest
      port: 8087
      ...
```

**Конвенция:** ключ карты — **логическое имя** (для шагов и API типа `containerManager.baseUrl("ctrl")`). Поле `name` внутри — **физическое имя контейнера** (для `--name` в docker/podman и префикса лог-файла). В большинстве случаев совпадают.

**Если карта `test.services` отсутствует или пуста** — `ContainerManager` ничего не поднимает (R-6).

### 3.2. Жизненный цикл контейнеров

`ContainerManager` должен:

1. На старте Spring-контекста (`@PostConstruct`) прочитать карту `test.services`.
2. Для каждого сервиса с `enabled: true`:
   - Создать и запустить контейнер.
   - Запустить log-capture в фоне (писать в `logs/<name>-<date>.log`).
3. Параллельно дождаться health-check для всех запущенных контейнеров.
4. На остановке Spring-контекста (`@PreDestroy`):
   - Остановить все log-capture процессы.
   - Остановить все контейнеры.

### 3.3. Программный API для шагов

`ContainerManager` должен предоставить:

- **`baseUrl(serviceKey)`** — вернуть URL вида `http://localhost:<port>` для указанного ключа.
- **`container(serviceKey)`** — прямой доступ к `GenericContainer` (для продвинутых кейсов: `execInContainer`, `copyFileFromContainer`).
- Оба метода бросают понятную ошибку при обращении к неизвестному ключу или к отключённому сервису.

### 3.4. Изменения в документации

| Файл | Что должно получиться |
|------|------------------------|
| `docs/configuration.md` | Раздел `test.service` (singular) **удалён**. Добавлен раздел `test.services` (plural, Map) с описанием всех полей. |
| `docs/infrastructure.md` | Раздел «Контейнер тестируемого сервиса» переписан под N сервисов; добавлена диаграмма (3 SUT'а + инфра). |
| `README.md` | Quickstart переписан: пример с 2 SUT-сервисами. |
| `CHANGELOG.md` | Запись о breaking change: `test.service` → `test.services`, миграция обязательна. |

### 3.5. Изменения в smoke-тестах библиотеки

- **`smoke/`** — переписан под plural-схему: `test.services.main: { ... }` (один сервис, но через plural-инфру).
- **`smoke-multi/`** (новый) — модуль с 2 SUT-сервисами и одной фичей, проверяющей взаимодействие между ними. Это regression-тест plural-схемы.

---

## 4. Ожидания потребителя (что библиотека должна дать)

### 4.1. Стратегия миграции в потребителях

**Breaking change** (B-1): singular-схема `test.service` удаляется полностью, без алиасов. Все потребители библиотеки **обязаны** перейти на `test.services`.

**Версия библиотеки:** bump major (`1.0.0-SNAPSHOT` → `2.0.0-SNAPSHOT`). SemVer: breaking change в публичном API конфигурации.

**Smoke-тест `smoke/`** также мигрирует на plural-схему (один сервис, но описанный через `test.services.main: { ... }`). Это не «обратная совместимость», а просто форма записи — внутри всё тот же один контейнер.

### 4.2. Концептуальная разница в YAML для потребителя

**Было** (текущее состояние потребителя — нашего и любого другого):

```yaml
test:
  service:                   # singular — обёртка над одним сервисом
    enabled: true
    startup-timeout-seconds: 180
  services:                  # самописный Map в обход библиотеки (наш проект)
    ctrl:    { base-url: http://localhost:8085, health-path: /actuator/health }
    distr:   { base-url: http://localhost:8086, health-path: /actuator/health }
    simulation: { base-url: http://localhost:8087, health-path: /actuator/health }
```

**Должно стать:**

```yaml
test:
  services:
    ctrl:
      enabled: true
      name: ctrl
      image: strategy-rules-ctrl:latest
      port: 8085
      startup-timeout-seconds: 180
      health-path: /actuator/health
      env: { SPRING_PROFILES_ACTIVE: local }
    distr:
      enabled: true
      name: distr
      image: strategy-rules-distr:latest
      port: 8086
      startup-timeout-seconds: 180
      health-path: /actuator/health
      env: { SPRING_PROFILES_ACTIVE: local, CTRL_URL: http://localhost:8085 }
    simulation:
      enabled: true
      name: simulation
      image: strategy-rules-simulation:latest
      port: 8087
      startup-timeout-seconds: 180
      health-path: /actuator/health
      env: { SPRING_PROFILES_ACTIVE: local, DISTR_URL: http://localhost:8086 }
```

### 4.3. Что потребитель больше не должен делать сам

После перехода на новую схему потребитель освобождается от:

- Самостоятельного подъёма контейнеров — библиотека делает сама по описанию в YAML.
- Самостоятельного health-check waiter'а — библиотека делает сама.
- Самостоятельной настройки log-capture — библиотека делает сама (отдельный файл на сервис).

Самописные обходные конфигурации в потребителях становятся не нужны и могут быть удалены.

---

## 5. Открытые вопросы (требуют уточнения у мейнтейнеров библиотеки)

| # | Вопрос | Варианты |
|---|--------|----------|
| Q1 | ~~Singular-схема: оставить как алиас или удалить?~~ | **РЕШЕНО (B-1):** singular удаляется полностью, без алиасов. |
| Q2 | Что делать с конфликтом ключей в `test.services` (например, дубликаты через YAML merge)? | (а) первый побеждает. (б) `IllegalStateException` — fail-fast. |
| Q3 | Параллельный `start()`: всегда параллельно, или сделать флаг `parallel-start` (дефолт `true`)? | Предлагаю всегда параллельно. |
| Q4 | ~~Поле `name` в `ServiceConfig`: обязательное или опциональное?~~ | **РЕШЕНО (B-2):** обязательное. |
| Q5 | Log-capture: оставить префикс по имени сервиса (`logs/<name>-<date>.log`) или общий `service-<date>.log`? | Префикс по имени — логи не смешиваются, легче дебажить. |
| Q6 | `port` в `services.<name>.port` — это fixed port (host-сеть) или exposed port (random для проброса)? | Известные потребители используют host-сеть → `localhost:<fixed>`. Для bridge-сети нужен проброс. Предлагаю фиксировать port + поддержать `getMappedPort()`. |

---

## 6. Критерии приёмки

### В библиотеке

- [ ] Singular-схема `test.service` **удалена** из конфигурации (поле отсутствует, YAML-схема `test.service` не парсится) — B-1.
- [ ] Поле `name` в `ServiceConfig` обязательно (валидация на старте с понятной ошибкой) — B-2.
- [ ] Потребитель с `test.services` (plural, 2+ сервиса) поднимает все контейнеры параллельно (R-1).
- [ ] Health-check выполняется параллельно; суммарное время ≈ `max(startupTimeoutSeconds)` (R-2).
- [ ] Каждый сервис получает отдельный файл лога `logs/<name>-<date>.log` (R-3).
- [ ] Step-definitions могут получить `baseUrl(name)` через DI бина `ContainerManager` (R-4).
- [ ] `test.services.<name>.enabled: false` корректно отключает подъём конкретного сервиса (R-5).
- [ ] Пустая/отсутствующая карта `test.services` → `ContainerManager` ничего не делает (R-6).
- [ ] `smoke/` мигрирован на plural (`test.services.main`) и зелёный.
- [ ] Новый `smoke-multi/` (2 сервиса) зелёный.
- [ ] Документация обновлена: `docs/configuration.md` (без `test.service`), `docs/infrastructure.md`, `README.md`, `CHANGELOG.md` с записью о breaking change.
- [ ] Версия библиотеки bumpнута: `1.0.0-SNAPSHOT` → `2.0.0-SNAPSHOT`.

### В потребителях

- [ ] Все потребители библиотеки bumpнули версию зависимости до `2.0.0-SNAPSHOT`.
- [ ] Все потребители мигрировали YAML на `test.services` (с обязательным `name`).
- [ ] Самописные обходные конфигурации (если были) удалены.
- [ ] Step-definitions получают URL через `ContainerManager.baseUrl(...)`.
- [ ] Полный набор BDD-тестов потребителя зелёный.

---

## 7. Связанные файлы (в библиотеке)

- `core/src/main/java/io/opentest/bdd/ContainerManager.java` — переработать под N контейнеров.
- `core/src/main/java/io/opentest/bdd/TestConfigProperties.java` — удалить singular-поле, добавить `services` (Map), сделать `name` обязательным.
- `smoke/src/test/resources/application-test.yml` — мигрировать на plural.
- `smoke-multi/` (новый) — regression-тест plural-схемы.
- `docs/configuration.md`, `docs/infrastructure.md`, `README.md`, `CHANGELOG.md` — документация.
- `pom.xml` (корень библиотеки) — bump версии.

---

## 8. Альтернативы, которые **не** выбрали

| Альтернатива | Почему отвергли |
|--------------|-----------------|
| Не трогать библиотеку — каждый потребитель пишет свой велосипед под N сервисов | Дублирование log-capture/health-check/lifecycle во всех потребителях. Техдолг. При улучшениях в библиотеке все отстают. |
| Сделать `services` хуком через SPI (`ServiceContainerProvider`) | Избыточно для текущей задачи (все известные потребители используют один runtime — Testcontainers на host-сети). Если появятся разные runtime (Quarkus, native-image) — можно вернуться к SPI. |
| Форкнуть библиотеку | Плохо для сопровождения. Предпочитаем upstream-контрибьюцию. |
| Оставить singular как deprecated-алиас (с предупреждением в логах) | Заказчик явно зафиксировал отсутствие требования обратной совместимости. Дублирующий код в `ContainerManager` (singular-ветка + plural-ветка) — лишняя сложность. |
| Делать singular схемой по умолчанию с plural как расширением | Неестественно: N=1 — частный случай N≥1, а не наоборот. Plural как primary схема единообразнее. |
