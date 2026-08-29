# Standalone Telemt MTProto Proxy

Нативный установщик Telemt для Ubuntu 24.04 x86_64 без Docker. Он следует
[официальной ручной systemd-инструкции](https://github.com/telemt/telemt/blob/main/docs/Quick_start/QUICK_START_GUIDE.ru.md#telemt-%D1%87%D0%B5%D1%80%D0%B5%D0%B7-systemd-%D0%B2%D1%80%D1%83%D1%87%D0%BD%D1%83%D1%8E),
но добавляет YAML-конфигурацию, проверку релиза, backup/rollback, безопасные
permissions, health-check и отдельный cleaner.

Это неофициальный установщик. Бинарник Telemt в ZIP не включён: setup скачивает
официальный закреплённый релиз и проверяет SHA-256.

## Быстрый запуск

На новой Ubuntu VM:

```bash
unzip telemt-setup-standalone-1.2.3.zip
cd telemt-setup
cp config.example.yaml config.yaml
nano config.yaml
sudo ./setuptelemt.sh all
```

В `config.yaml` обязательно замените `links.public_host` на публичный IPv4 или
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
`install.service_name` и откажется продолжать при конфликте unit или порта.

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
Сгенерированный `/etc/telemt/telemt.toml` устанавливается как
`root:telemt 0640`. Ссылки не пишутся в setup-output или journal.

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

Пользовательские SOCKS5-записи остаются без `scopes`. В Telemt 3.5.5 запрос без
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

SOCKS5 совместим с `proxy.use_middle_proxy: true` в закреплённом Telemt 3.5.5:
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
journalctl -u telemt.service --since "10 minutes ago"
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
повторите `sudo ./setuptelemt.sh all`. Архитектура и libc должны совпадать с VM.
Floating `latest` намеренно не используется.

## Очистка

Без `--yes` cleaner только показывает план:

```bash
./cleantelemt.sh
sudo ./cleantelemt.sh --yes
sudo ./cleantelemt.sh --yes --purge-user --purge-setup
```

Дополнительные варианты:

```bash
sudo ./cleantelemt.sh --yes --keep-config --keep-data --keep-backups
sudo ./cleantelemt.sh --yes --purge-ufw
sudo ./cleantelemt.sh --yes --config /secure/path/telemt.yaml
```

Cleaner удаляет только unit, binary, config, data, state и backups выбранного
instance. Он не запускает `apt autoremove`, не очищает общий journal, не меняет
cloud security group и не затрагивает другие экземпляры Telemt.

## Состав архива

- `setuptelemt.sh`, `cleantelemt.sh`, `config.example.yaml`
- модульные `steps/00..03`, общие shell-функции и Python tools
- `README.md`, `VERSION`, `THIRD_PARTY_NOTICES.md`

В ZIP намеренно отсутствуют binary Telemt, `config.yaml`, secrets, venv, Git,
PCAP, runtime cache и отчёты тестовых прогонов.

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
- Upstream manager Telemt 3.5.5: <https://github.com/telemt/telemt/blob/3.5.5/docs/Advanced_settings/TUNING.en.md>
- `tls_fetch_scope` и `scopes` Telemt 3.5.5: <https://github.com/telemt/telemt/blob/3.5.5/docs/Config_params/CONFIG_PARAMS.ru.md>
- Readiness API Telemt 3.5.5: <https://github.com/telemt/telemt/blob/3.5.5/docs/Architecture/API/API.md>
- Лицензия: <https://github.com/telemt/telemt/blob/main/LICENSE>
