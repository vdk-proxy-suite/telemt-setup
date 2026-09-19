# Standalone Telemt MTProto Proxy

Нативный установщик [Telemt](https://github.com/telemt/telemt) для Ubuntu 24.04
x86_64 без Docker. Он следует
[официальной ручной systemd-инструкции](https://github.com/telemt/telemt/blob/main/docs/Quick_start/QUICK_START_GUIDE.ru.md#telemt-%D1%87%D0%B5%D1%80%D0%B5%D0%B7-systemd-%D0%B2%D1%80%D1%83%D1%87%D0%BD%D1%83%D1%8E),
но добавляет YAML-конфигурацию, проверку релиза, backup/rollback, безопасные
permissions, health-check и отдельный cleaner.

Это неофициальный установщик. Бинарник Telemt в ZIP не включён: setup скачивает
официальный закреплённый релиз и проверяет SHA-256.

## Быстрый запуск

На новой Ubuntu VM сначала установите зависимости CLI:

```bash
sudo apt-get update
sudo apt-get install --no-upgrade -y python3 python3-yaml
```

Затем:

```bash
unzip telemt-setup-standalone-1.3.1.zip
cd telemt-setup
cp config.example.yaml config.yaml
nano config.yaml
sudo ./setuptelemt.sh all
```

В `config.yaml` задайте уникальный на этой VM `instance.id` (например `main`)
и обязательно замените `links.public_host` на публичный IPv4 или
DNS-имя VM. Пример по умолчанию поднимает TLS-only MTProto Proxy на TCP/443 с
Fake-TLS/SNI `petrovich.ru`, Middle Proxy и общим лимитом 256 соединений.

Готовые ссылки содержат credentials и поэтому выводятся только явно:

```bash
sudo ./setuptelemt.sh links
```

Можно хранить YAML вне распакованного каталога:

```bash
sudo ./setuptelemt.sh all --config /secure/path/telemt.yaml
sudo ./setuptelemt.sh links --config /secure/path/telemt.yaml
```

## Модульные шаги

```bash
sudo ./setuptelemt.sh 0  # preflight, inventory, backup, stop только target unit
sudo ./setuptelemt.sh 1  # зависимости, официальный binary, checksum/version
sudo ./setuptelemt.sh 2  # защищённый TOML и systemd unit
sudo ./setuptelemt.sh 3  # enable/start и обязательный VM health-check
```

`all` запускает шаги последовательно и выполняет rollback при ошибке. Шаг 0
никогда не останавливает все процессы Telemt: он работает только с
`instance.id` и откажется продолжать при конфликте unit, владельца или порта.
Чужой listener проверяется по фактическому PID до остановки своего unit. Шаги
1 и 2 отказываются работать поверх активного экземпляра; после 0 → 1 → 2 он
остаётся остановленным, шаг 3 включает его и проверяет готовность.

## Пользователи и секреты

Можно задать несколько независимых ссылок:

```yaml
users:
  family:
    secret: "GENERATE"
    ad_tag: null
    max_unique_ips: null
  private:
    secret: "<32_HEX_SECRET>"
    ad_tag: null
    max_unique_ips: 2
```

`<32_HEX_SECRET>` в примере нужно заменить на собственные 32 hex-символа.
`GENERATE` создаёт 16 случайных байт и сохраняет тот же secret при повторном
setup для существующего имени. Удаление установки с конфигом означает, что при
следующей чистой установке будет создана новая ссылка.

Если YAML содержит явный secret или `ad_tag`, установщик требует `chmod 600`.
Сгенерированный `/etc/telemt-setup/instances/<id>/telemt.toml` устанавливается
как `root:telemt-<id> 0640`. Ссылки не пишутся в setup-output или journal.

Смена `proxy.tls_domain` делает ранее выданные TLS-ссылки недействительными —
после неё пользователям нужно выдать новые ссылки из локального API.

## Исходящий SOCKS5 для MTProto

Опциональный корневой список `upstreams` направляет через SOCKS5 только основной
исходящий маршрут Telemt к Telegram: MTProto/DC, Telegram Middle-End и служебные
ME-запросы. Он не настраивает прокси для сторонних приложений или Telegram Bot
API. Доступ SOCKS5 к `proxy.tls_domain` не требуется.

Чтобы жёстко направить Telegram-трафик Telemt через один внешний SOCKS5,
добавьте:

```yaml
upstreams:
  - type: "socks5"
    address: "proxy.example.invalid:1080"
    username: "<PROXY_USER>"
    password: "<PROXY_PASSWORD>"
    weight: 1
    enabled: true
```

Поля `username` и `password` можно вместе удалить для SOCKS5 без авторизации.
Если `upstreams` отсутствует или равен `[]`, Telemt использует обычный прямой
маршрут без дополнительных scoped-записей.

При одновременно включённом `proxy.tls_emulation` и хотя бы одном enabled
SOCKS5 установщик автоматически генерирует отдельный внутренний маршрут:

```toml
[censorship]
tls_fetch_scope = "telemt_setup_tls_front_direct"

[censorship.tls_fetch]
strict_route = true

[[upstreams]]
type = "direct"
scopes = "telemt_setup_tls_front_direct"
weight = 1
enabled = true
```

Пользовательские SOCKS5-записи остаются без `scopes`. В Telemt 3.5.7 запрос без
scope может выбрать только unscoped-запись, а запрос с scope — только запись с
точно совпадающим тегом. Поэтому Telegram/ME не видит внутренний `direct`, а
TLS-front metadata bootstrap/refresh не видит SOCKS5. `strict_route = true`
запрещает TLS-fetch скрыто переходить на другой маршрут при ошибке.

Это жёсткое поведение установщика и оно не добавляет внешний YAML-параметр:
`direct`, `scopes` и `tls_fetch_scope` нельзя задавать через `config.yaml`.
Обычная маскировочная переадресация неизвестного TLS-клиента к домену также
выполняется самим Telemt напрямую с VM и не использует upstream manager.
`direct` здесь означает системный DNS и таблицу маршрутизации VM; transparent
proxy или policy routing самой ОС находятся вне контроля Telemt.

Установщик намеренно не добавляет unscoped direct fallback рядом с SOCKS5:
иначе Telegram-трафик мог бы незаметно вернуться на публичный IP VM. Если
`proxy.tls_emulation: false`, TLS metadata fetch отсутствует и внутренний scoped
direct не создаётся. Конфигурации без `proxy.tls_domain` при выключенном TLS-mode
также поддерживаются: поле не выводится в TOML; если TLS-emulation всё же
включена, Telemt использует свой домен по умолчанию и получает его метаданные
через тот же внутренний direct route.

SOCKS5 совместим с `proxy.use_middle_proxy: true` в закреплённом Telemt 3.5.7:
этот маршрут применяется и к TCP-соединениям с Telegram Middle-End. Для ME
удалённый SOCKS5 должен возвращать корректный публичный `BND.ADDR` и ненулевой
`BND.PORT`; обычная проверка через `curl --socks5` этого не подтверждает.

YAML с SOCKS5-паролем должен иметь режим `0600`. Сохраняйте
`proxy.log_level: "normal"`: Telemt может показать структуру upstream с
credentials в подробных debug/verbose-логах.

## Firewall и эксплуатация

Для production-развёртывания откройте в cloud security group только входящий
TCP/443 и отдельно нужный диапазон для SSH. Telemt не использует UDP. API
`127.0.0.1:9091` и Prometheus `127.0.0.1:9090` не должны публиковаться наружу.

Установщик не меняет cloud firewall и по умолчанию не меняет UFW.
`install.manage_ufw: true` добавляет локальное allow-правило для proxy TCP-порта.
Не забудьте разрешить исходящий TCP к инфраструктуре Telegram и GitHub во время
установки. При настроенном `upstreams` VM также должен иметь TCP-доступ к адресу
SOCKS5, а при `proxy.tls_emulation: true` — прямой TCP/443 и DNS-доступ к
`proxy.tls_domain` либо к домену Telemt по умолчанию.

Проверки на VM:

```bash
sudo python3 tools/healthcheck.py --scope vm --config config.yaml
curl -fsS http://127.0.0.1:9090/metrics
sudo tail -n 100 /var/log/telemt/instances/main/telemt.log
```

Внешний Fake-TLS smoke-test с клиентской машины:

```bash
python -m venv venv
venv/bin/pip install PyYAML
venv/bin/python tools/healthcheck.py --scope e2e --config config.yaml
```

В Windows PowerShell используйте `venv\Scripts\python.exe`. E2E проверяет
публичный TCP, SNI и Fake-TLS handshake. API/metrics и runtime-логи подтверждают
готовность Telemt и Middle Proxy. VM healthcheck также требует успешный ответ
`/v1/health/ready`. Если в YAML есть SOCKS5, он отдельно проверяет
`/v1/runtime/upstream-quality` и требует хотя бы один healthy unscoped SOCKS5:
для него должно существовать фактическое успешное DC-наблюдение latency, поэтому
ни начальное `healthy=true`, ни внутренний TLS-direct не могут скрыть отказ
Telegram-маршрута. Без авторизованного Telegram-клиента тест всё равно не
доказывает доставку сообщений.

## Обновление

Укажите новую точную версию и SHA-256 официального release asset в YAML, затем
выполните `sudo ./setuptelemt.sh update --config config.yaml`.
При ошибке восстанавливаются прежние binary, TOML, YAML, unit, logrotate,
установленный код setup и предыдущее active/enabled состояние экземпляра. Архитектура и libc должны совпадать с VM.
Floating `latest` намеренно не используется.

## Изоляция экземпляров и управление

Один ZIP распаковывается в отдельные каталоги. В каждом свой `config.yaml` и
обязательный `instance.id`: строчная латинская буква, затем буквы, цифры или
дефисы, всего не более 20 символов. ID не выводится из имени каталога. Соседям
задайте разные proxy/API/metrics-порты; API и metrics допускают только loopback.
Порты зарегистрированных остановленных экземпляров также считаются занятыми.
Параллельные setup не поддерживаются.

Unit/user/group и пути вычисляются из ID; отличающиеся переопределения
`install.service_name`, `user`, `group` и путей отклоняются.

| Ресурс | Путь / имя |
|---|---|
| Unit, user, group | `telemt-<id>.service`, `telemt-<id>` |
| TOML | `/etc/telemt-setup/instances/<id>/telemt.toml` |
| Binary | `/opt/telemt-setup/instances/<id>/bin/telemt` |
| Runtime data | `/var/lib/telemt/instances/<id>` |
| Runtime directory | `/run/telemt-<id>` |
| Логи | `/var/log/telemt/instances/<id>/telemt.log` |
| Logrotate | `/etc/logrotate.d/telemt-<id>` |
| Manifest, защищённый YAML, backups, staging | `/var/lib/telemt-setup/instances/<id>` |
| Установленный код setup | `/usr/local/lib/telemt-setup/instances/<id>` |

Новые корни `/etc/telemt-setup` и `/opt/telemt-setup` намеренно отделены от
старых `/etc/telemt` и `/opt/telemt`: legacy-пользователь владеет рабочим
каталогом, а его группа ограничивает доступ к старому конфигу. Setup не меняет
владельца и permissions этих родительских каталогов при установке нового ID.

Root-owned JSON manifest создаётся до системных ресурсов. Он хранит владение,
UID/GID, происхождение распаковки и незавершённые операции. Чужой существующий
unit/account, подменённые пути, symlink, hardlink управляемого файла и неизвестные
drop-ins приводят к отказу. Стандартный `/var/log` root:syslog допускается как
доверенный системный родитель; собственные корни экземпляров защищены от записи
посторонними пользователями.

Повторный setup из исходного каталога управляет тем же экземпляром. Перенос или
копирование каталога с тем же ID требует явного принятия новой распаковки:

```bash
sudo ./setuptelemt.sh update --config config.yaml --update-existing
```

Системная идентичность и secrets сохраняются. Новый экземпляр требует нового
ID. Старый сервис с YAML без `instance` запускается только с `--legacy`, например
`sudo ./setuptelemt.sh all --legacy --config /secure/legacy.yaml`. Это сохраняет
его прежние пути и secrets; автоматического присвоения старых ресурсов нет.
Legacy cleanup также требует `--legacy` и проверяет отсутствие пересечения с
ресурсами зарегистрированных экземпляров. Обычная legacy-установка и новый
именованный экземпляр могут работать рядом на разных портах.

Для переноса создайте новый ID с отдельными портами и перенесите нужные secrets
из старого защищённого TOML в YAML с режимом `0600`. Проверьте новые ссылки,
после чего отдельно выведите старую установку через её явный legacy-cleanup.
Изменение TLS-домена требует новых Fake-TLS ссылок. `GENERATE` нового ID не
импортирует старые secrets автоматически.

Сервис и установленный setup работают после удаления исходной распаковки:

```bash
sudo /usr/local/lib/telemt-setup/instances/main/setuptelemt.sh status --instance main
sudo /usr/local/lib/telemt-setup/instances/main/setuptelemt.sh stop --instance main
sudo /usr/local/lib/telemt-setup/instances/main/setuptelemt.sh start --instance main
sudo /usr/local/lib/telemt-setup/instances/main/setuptelemt.sh backup --instance main
sudo /usr/local/lib/telemt-setup/instances/main/setuptelemt.sh healthcheck --instance main
sudo /usr/local/lib/telemt-setup/instances/main/setuptelemt.sh links --instance main
```

`all`, `update`, `reconfigure`, `0`–`3` и отдельные `steps/*.sh` принимают тот же
выбор `--instance`/`--config`. При обоих аргументах ID должны совпадать.
`reconfigure` применяет YAML без повторного скачивания binary. `stop`, `status`
и `cleanup --instance` работают по manifest даже без YAML. `backup` не
останавливает сервис. Ссылки выводятся только явной командой `links`.

Официальный tar.gz можно передать через
`--release-archive /secure/telemt-x86_64-linux-gnu.tar.gz`; проверка закреплённого
SHA-256 и версии остаётся обязательной. Временные загрузки находятся в state
этого экземпляра. Существующие пакеты setup не обновляет; устанавливает только
отсутствующие зависимости с `apt-get --no-upgrade`.

`install.manage_ufw: true` добавляет маркированное правило только при отсутствии
готового правила для этого порта. Прежнее правило не становится собственностью
setup. Удаление своего правила требует отдельного cleanup-флага и проверки
потребителей. Общие UFW настройки и cloud firewall не изменяются.

## Очистка

Без `--yes` cleaner только показывает план и проверяет владельца:

```bash
sudo ./cleantelemt.sh --instance main
sudo ./cleantelemt.sh --instance main --yes
sudo ./cleantelemt.sh --instance main --yes --purge-logs
sudo ./cleantelemt.sh --instance main --yes --purge-shared-components --purge-setup
```

Обычная очистка останавливает свой unit и удаляет его, binary, TOML, data,
runtime, backups и logrotate. Собственные user/group удаляются, если нет
оставленных config/data, чужих процессов, других аккаунтов группы или чужих
unit, использующих эту идентичность. Логи сохраняются с владением root.

- `--purge-logs` удаляет только каталог логов выбранного экземпляра
- `--purge-shared-components` разрешает удалить доказанно собственное UFW
  правило; системные пакеты сохраняются, поскольку их исключительное владение
  и отсутствие внешних потребителей доказать нельзя
- `--purge-ufw` отдельно разрешает ту же проверку удаления UFW правила
- `--purge-setup` удаляет исходный каталог распаковки только при совпадении
  marker, ID, inode и зарегистрированного пути; Git checkout, подменённый или
  смонтированный каталог не удаляется; при дополнительных файлах (например,
  setup.log) распаковка сохраняется до совместного --purge-logs
- `--keep-config`, `--keep-data`, `--keep-backups`, `--keep-user` сохраняют
  ресурсы; `--purge-user` оставлен как совместимый явный запрос с теми же
  проверками потребителей

Manifest и установленный cleaner сохраняются для повторной очистки, включая
`--purge-logs` после удаления YAML и исходной распаковки. Cleanup из новой
распаковки с `--instance main` безопасно повторяется. Общий journal никогда не
очищается, `apt autoremove` не выполняется, чужие unit/user/файлы не удаляются.

## Состав архива

- `setuptelemt.sh`, `cleantelemt.sh`, `config.example.yaml`
- модульные `steps/00..03`, общие shell-функции и Python tools
- `README.md`, `VERSION`, `THIRD_PARTY_NOTICES.md`

В ZIP намеренно отсутствуют binary Telemt, `config.yaml`, secrets, venv, Git,
PCAP, runtime cache и отчёты тестовых прогонов.

## Изменения версии 1.3.1

- Исправлен порядок проверки владения UFW: повреждённая запись правила или
  marker другого экземпляра отклоняются до остановки unit и удаления файлов
- Одна проверка manifest применяется к lifecycle, cleanup dry-run и удалению
  правила; корректные правила и существующая политика сохранения не изменены

## Изменения версии 1.3.0

- Сквозная изоляция по `instance.id`: user/unit, binary, TOML, data, staging,
  backups, логи и установленный код
- Единый выбор экземпляра и полный lifecycle, включая отдельные шаги
- JSON ownership manifest, защита конфликтов и rollback при ошибке
- Работа после удаления распаковки, повторный cleanup без YAML, отдельные
  флаги удаления логов и общих компонентов
- Явный legacy-режим без автоматического присвоения старых ресурсов

## Изменения версии 1.2.5

- Закреплён официальный Telemt 3.5.7 для Ubuntu x86_64 GNU
- SHA-256 release asset сверен с официальным checksum-файлом, GitHub asset
  digest и независимо вычисленным локальным хешем
- Сохранена прежняя YAML-схема и разделение direct/SOCKS5/scoped TLS-front;
  дополнительные WEB-возможности не включаются

## Изменения версии 1.2.4

- В шапке README добавлена ссылка на оригинальный репозиторий Telemt
- Закреплённая версия Telemt и поведение установщика не изменены

## Изменения версии 1.2.3

- Закреплён официальный Telemt 3.5.5 для Ubuntu x86_64 GNU
- SHA-256 release asset сверен с официальным checksum-файлом, GitHub asset
  digest и независимо вычисленным локальным хешем
- Подтверждена совместимость strict TOML, SOCKS5/ME, scoped direct для TLS-front,
  readiness и upstream-quality API без изменения внешней YAML-схемы
- В Telemt переименованы четыре пользовательских Prometheus-счётчика с
  добавлением суффикса `_total`; healthcheck установщика от их имён не зависит,
  но внешние Prometheus/Grafana-запросы могут потребовать обновления

## Изменения версии 1.2.2

- Без изменения внешнего YAML добавлено жёсткое разделение маршрутов: Telegram
  DC/ME остаются на unscoped SOCKS5, TLS-front metadata fetch использует только
  внутренний scoped direct с `strict_route = true`
- Маскировочная переадресация и TLS-front обращения документированы как прямой
  egress VM; SOCKS5 больше не обязан разрешать доступ к `tls_domain`
- Сохранено прежнее поведение без upstream; исправлен рендеринг конфигураций без
  явно заданного `tls_domain`. Scoped direct создаётся только при TLS-emulation
- VM healthcheck теперь требует healthy unscoped SOCKS5 с наблюдаемой DC latency
  и не принимает начальный health или TLS-direct за исправный Telegram-маршрут

## Изменения версии 1.2.1

- Закреплён официальный Telemt 3.5.3 для Ubuntu x86_64 GNU
- SHA-256 release asset обновлён и независимо сверен с официальным checksum
- Сгенерированный strict TOML, SOCKS5/ME и readiness API проверены на
  совместимость без изменения конфигурации

## Изменения версии 1.2.0

- Добавлен опциональный список SOCKS5 `upstreams` с генерацией корневых
  `[[upstreams]]` в Telemt TOML
- Существующие YAML без `upstreams` сохраняют прямой исходящий маршрут
- Добавлены строгая проверка endpoint/credentials, требование `chmod 600` для
  YAML с паролем и unit-тесты генератора
- VM healthcheck теперь проверяет readiness Telemt и наличие здорового upstream

## Изменения версии 1.1.1

- Production-профили `config.<ssh-alias>.yaml` и локальный `AGENTS.md`
  исключаются из Git и standalone ZIP
- Release builder исключает все боевые `config*.yaml`, сохраняя публичный
  `config.example.yaml`

## Изменения версии 1.1.0

- исправлена проверка GNU libc при включённом `pipefail`: `ldd` теперь не
  получает ложный `SIGPIPE` от комбинации `head` и `grep -q`
- повторный `setuptelemt.sh all` после обновления системной glibc снова
  корректно применяет технические изменения YAML с сохранением secrets

## Источники

- Telemt: <https://github.com/telemt/telemt>
- Актуальный пример конфига: <https://github.com/telemt/telemt/blob/main/config.toml>
- FAQ: <https://github.com/telemt/telemt/blob/main/docs/FAQ.ru.md>
- Upstream manager Telemt 3.5.7: <https://github.com/telemt/telemt/blob/3.5.7/docs/Advanced_settings/TUNING.en.md>
- `tls_fetch_scope` и `scopes` Telemt 3.5.7: <https://github.com/telemt/telemt/blob/3.5.7/docs/Config_params/CONFIG_PARAMS.ru.md>
- Readiness API Telemt 3.5.7: <https://github.com/telemt/telemt/blob/3.5.7/docs/Architecture/API/API.md>
- Лицензия: <https://github.com/telemt/telemt/blob/main/LICENSE>
