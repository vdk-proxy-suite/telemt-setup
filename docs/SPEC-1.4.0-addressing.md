# Спецификация telemt-setup 1.4.0: IPv4, IPv6 и DNS в адресах и ссылках

Историческая спецификация, согласованная перед реализацией. База: telemt-setup
1.3.2, закреплённый Telemt 3.5.16. Ниже сохранён первоначальный план; актуальный
контракт реализованного 1.4.0 описан в README, отличия приведены здесь отдельно.

Примечание реализации 1.4.0: добавлен явный `outbound.families/prefer`, default
IPv4, чтобы входящие binds не меняли Telegram route. Подтверждены native
IPV6_V6ONLY и несколько listeners. Strict parser 3.5.16 отвергает upstream.prefer
вопреки reference: используется network.prefer и upstream ipv4/ipv6. Для v2
TLS-front получает отдельный IPv4 scoped route также без пользовательских SOCKS.
Актуальный пользовательский контракт находится в README, dev-приёмка — DEV-1.4.0.md.

## 1. Цель и текущее поведение

Один instance принимает клиентов по IPv4/IPv6 на прежнем порту и выдаёт для
каждого пользователя варианты ссылки: DNS, IPv4, IPv6. Добавление адреса не
создаёт нового пользователя и не меняет его secret.

Домен уже поддерживается: `links.public_host` принимает IP или DNS и попадает
в `[general.links].public_host`. Для одной доменной ссылки изменение схемы
не требуется. Однако это не включает IPv6 listener автоматически.

Ограничения текущего кода:

- `tools/config.py`: единственный `server.listen_ip`, единственная пара
  `links.public_host/public_port`; рендерится один `[[server.listeners]]`
- Валидатор уже принимает IPv6 literal, но это не подтверждает поддержку
  всего IPv6 lifecycle; `[network]` установщик сейчас не задаёт
- `tools/healthcheck.py`: VM проверяет один bind; wildcard преобразуется в
  loopback только для IPv4; URL API/metrics не заключает IPv6 в квадратные скобки
- E2E использует `socket.create_connection` без отдельного результата для каждой
  семьи/адреса; успешный IPv4 может скрыть отсутствие IPv6
- `links` сейчас печатает ответ `/v1/users`, собственного каталога адресов нет
- `tools/instance.py`: резервирование целых портов, один `ufw_rule` в manifest

## 2. Разделение сущностей

1. Bind: локальный IP, на котором Telemt слушает TCP. Только literal, не DNS
2. Public endpoint: IP или DNS и внешний порт в клиентской ссылке; может
   отличаться от bind при NAT
3. `proxy.tls_domain`: маскировочный Fake-TLS/SNI домен; не адрес подключения
4. Исходящий маршрут: direct либо SOCKS5 к Telegram; не определяется семьёй
   входящего соединения и не меняется из-за домена в ссылке

Для обычного MTProto Fake-TLS эта работа не добавляет ACME/Let's Encrypt и
не меняет `tls_domain`. WEB transport, HTTPS termination и сертификаты для
собственного веб-домена — отдельная задача.

## 3. Предлагаемый YAML v2

Ниже фрагмент; install/proxy/users/upstreams/probes сохраняются из полного профиля.
Использованы только документационные адреса.

```yaml
schema_version: 2
server:
  port: 443
  listen_ips: ["192.0.2.10", "2001:db8::10"]
  max_connections: 256
  api_listen: "127.0.0.1:9091"
  metrics_listen: "127.0.0.1:9090"

links:
  primary: dns
  endpoints:
    - id: dns
      host: proxy.example.org
      port: 443
      families: [ipv4, ipv6]
    - id: ipv4
      host: "192.0.2.10"
      port: 443
      families: [ipv4]
    - id: ipv6
      host: "2001:db8::10"
      port: 443
      families: [ipv6]
```

Контракты:

- Отсутствие schema_version означает прежнюю схему; неизвестная версия — ошибка
- v2 использует `listen_ips` вместо `listen_ip`, `primary/endpoints` вместо
  `public_host/public_port`; смешение форм отклоняется, без скрытого приоритета
- Один proxy-порт на instance сохраняется. Несколько произвольных портов внутри
  instance не нужны для этой задачи и не входят в релиз
- `listen_ips`: непустой список уникальных канонических IP; запрет DNS,
  multicast, zone-id и IPv4-mapped IPv6; public link не может быть wildcard
- API/metrics остаются исключительно loopback; IPv6 loopback разрешён при
  корректном форматировании URL и фактической доступности
- Endpoint ID уникален; primary ссылается на существующий endpoint. Порядок
  списка сохраняется, дубликаты host/port отклоняются
- Для IP `families` ровно его семья. Для DNS — непустое множество ipv4/ipv6;
  это обязательства проверки, не указания клиенту и не DNS-фильтр
- DNS валидируется как имя, нормализуется lowercase/IDNA; URL, userinfo,
  путь, встроенный порт и query запрещены. Offline validate не делает DNS-запросов
- DNS остаётся именем в YAML, TOML и ссылках. Setup не заменяет его текущим A/AAAA
- Public endpoint не обязан совпадать с локальным bind из-за NAT; несоответствие
  проверяет внешний E2E, а не ошибочный запрет на уровне схемы

## 4. TOML и граница с Telemt

`tools/config.py` нормализует обе YAML-схемы во внутреннюю модель адресов.
Для v2 каждый listen IP рендерится отдельным `[[server.listeners]]` с общим
портом. Primary endpoint идёт в `[general.links].public_host/public_port`.
Альтернативы — контракт установщика, не выдуманный TOML-массив public_host.

До реализации необходим небольшой compatibility spike на закреплённом binary:

- Проверить код и runtime 3.5.16: фильтрацию listeners через `network.ipv4/ipv6`,
  IPV6_V6ONLY и поведение `0.0.0.0` вместе с `::` на одном порту
- Проверить доступные `announce`, native API links и кодирование IPv6 в ссылках
- Установить, как разрешение входящего IPv6 влияет на выбор Telegram DC/ME и
  scoped TLS-front. Нельзя молча включить IPv6 egress у всех SOCKS-маршрутов
- По результату зафиксировать точный рендер network/upstream family flags и
  покрыть его fixtures. Если требуется публичное управление egress, оформить
  отдельный явный контракт, не выводить egress policy из links.endpoints

Для первой реализации базовый профиль использует два точных локальных IP.
Wildcard dual-stack включается только после проверки семантики binary.
Setup не меняет системный `net.ipv6.bindv6only`. Проблемная комбинация binds
отклоняется до остановки сервиса. `reuse_allow` не включается для обхода конфликта.

Сохранить `config_strict=true`, `show=[]`, локальные API/metrics, раздельные
unscoped SOCKS5 и scoped direct TLS-front. Цепочка через локальный 3proxy к
HTTP(S) parent сохраняет `use_middle_proxy=false`.

Обновлять Telemt до новой версии только ради этого автоматически не требуется.
Если spike обнаружит ограничение 3.5.16, изменение pin/version/checksum становится
отдельным явно описанным условием реализации и повторной регрессии.

## 5. Генерация ссылок и CLI

Старый `links` для v1 сохраняет прежний формат ответа API. Для v2 добавить
стабильный каталог вариантов с `schema_version: 2`: instance, primary,
пользователи и для каждого endpoint ID, host, port, mode, tg_url, https_url.
`links --endpoint ID` фильтрует варианты; `--format json|text` явно задаёт формат.
Аргументы прокидываются через setuptelemt.sh, instance.py и healthcheck.py.
Native `/v1/users` не патчится; сторонним потребителям он продолжает выдавать
native формат, каталог установщика — отдельный интерфейс.

Секрет брать из действующего TOML/API, а не из `GENERATE` в YAML. Рекомендуется
использовать secret из штатной ссылки API и менять только server/port через
URL parser, сохраняя режим и Fake-TLS суффикс. Формат и имена пользователей
сверяются с действующим instance; при рассогласовании — отказ, не новая генерация.

IPv6 host хранится без скобок; query кодируется URL-библиотекой. Не переносить
правило скобок из socket authority автоматически в `server=` query. Оба вида
ссылок (`tg://proxy` и `https://t.me/proxy`) проверяются на реальных клиентах.

Ссылки и secrets выводятся только явным `links`, не в setup/status/healthcheck,
не в обычные JSON-отчёты и journal. Возможность DNS/IPv6 transport и работа
импорта ссылки проверяются отдельно на Telegram Android/iOS/Desktop.

## 6. Healthcheck и DNS

Добавить общий `tools/addressing.py`: разбор IP/DNS, authority с IPv6-скобками,
нормализация, getaddrinfo с явной семьёй, дедупликация, проверка пересечений binds.

VM: проверка каждого bind, wildcard через loopback соответствующей семьи;
проверка фактического сокета и владельца PID, API, metrics, readiness и наблюдаемой
DC latency. Успех одного listener не заменяет остальные. Не удалять SOCKS check.

E2E: пройти каждый выбранный public endpoint, каждую объявленную семью и каждый
разрешённый A/AAAA адрес. Подключаться к конкретному IP, сохраняя SNI tls_domain.
Отсутствие AAAA при заявленном ipv6 — FAIL dns, а не тихое пропускание.
Разделять resolve/connect/handshake timeout; продолжать независимые проверки.
Один DNS snapshot на endpoint за прогон, ограничение общего времени и числа
адресов; при превышении лимита явно неполная проверка, не общий PASS.

CLI: `--endpoint ID`, `--family ipv4|ipv6|all`, `--timeout`, `--json PATH`.
Фильтры сужают проверку и отражаются в summary; исключённые семьи не считаются
проверенными. Принудительный IPv6 без маршрута у клиента остаётся FAIL connect
с понятной причиной. Отдельно отображать NO_ROUTE, TIMEOUT, DNS_MISSING,
TLS_SMOKE_FAILED, API_NOT_READY без credentials.

JSON v2: endpoint_id, address_family, connect_address, port, check, status,
error_kind, duration_ms; summary содержит passed/failed/skipped и scope/filters.
Ненулевой exit code при обязательном FAIL; PASS только при полной выбранной матрице.

Текущий E2E — обычное TLS-соединение с SNI и CERT_NONE без MTProto auth; это
smoke-test маскировочного TLS пути, не доказательство authenticated Fake-TLS или
доставки Telegram-сообщений. Исправить название/документацию. Приёмка обязательно
включает реальный Telegram-клиент с выбранной ссылкой.

## 7. Lifecycle, manifest и firewall

- Сохранить IDs, пути, пользователей, secrets, лимиты и порты старых инстансов
- Консервативное резервирование целого порта между instance оставить: расширять
  его до совместного порта разных instance в этом релизе не требуется
- До stop проверить все binds, API/metrics, конфликты и доступность нужных
  локальных IP. Иное семейство того же instance не считается чужим конфликтом
- Manifest расширить версионированными normalized binds и `ufw_rules` вместо
  единственного `ufw_rule`. Старую запись читать без присвоения чужих правил
- Правило UFW учитывает TCP, семью и destination bind; loopback не открывать
- Не менять глобальные IPv6/UFW настройки; при невозможности обеспечить
  заявленную семью — понятный preflight error
- Уже существующие правила сохранять без ownership adoption; учитывать UFW
  shorthand, объединяющий IPv4/IPv6, и не удалять его по одному family record
- При reconfigure удалять устаревшие собственные правила только после проверки
  потребителей; при rollback восстанавливать только изменения этой операции
- Backup/rollback включают schema/config, TOML, код, manifest и firewall delta;
  откат к v1 не должен оставить новые IPv6-правила или удалить старые общие
- Cleanup/dry-run проверяют каждое правило и каждого потребителя. Повторная
  очистка работает после удаления YAML; чужой marker означает сохранение ресурса
- Одна распаковка принадлежит одному instance; явно показать это в инструкции

## 8. Совместимость ссылок и миграция

Добавление DNS/IPv6 и смена primary не инвалидируют старую IPv4-ссылку при
сохранении IPv4 listener, порта, пользователя, secret, режима и tls_domain.
Старый клиент не узнает новый домен из уже сохранённой IP-ссылки — новый вариант
нужно импортировать отдельно. Смена tls_domain или secret требует новых ссылок.

DNS с A+AAAA предоставляет адреса клиенту; выбор семьи и fallback выполняет
клиент. Не обещать одинаковый Happy Eyeballs во всех Telegram-клиентах.

Миграция: отдельный тестовый ID/порты → runtime и клиентская проверка → backup
боевого instance → reconfigure с прежними secrets → проверка старой IPv4 и новых
DNS/IPv6 ссылок → только затем удаление тестового стенда. Боевые изменения
выполняются отдельно по разрешению пользователя.

## 9. Файлы и критерии приёмки

| Файл | Изменение |
| --- | --- |
| tools/config.py | v1/v2 validation, нормализация, TOML multiple listeners, primary |
| tools/addressing.py (новый) | IP/DNS/authority/resolution helpers |
| tools/healthcheck.py | Матрица VM/E2E, IPv6 URL, результаты/CLI, каталог ссылок либо вызов нового tools/links.py |
| tools/instance.py | Preflight binds, manifest migration, firewall ownership, rollback, CLI forwarding |
| setuptelemt.sh, steps/*, lib/* | Проверить передачу новых аргументов и отсутствие зависимостей от единственного listen_ip |
| tests/test_config.py | Совместимость v1, негативные v2 cases, TOML fixtures, неизменность secrets |
| tests/test_healthcheck.py | Обе семьи, нет AAAA, timeout, несколько A, loopback URL, отсутствие секретов |
| tests/test_instance.py | Conflict before stop, UFW union, чужой marker, rollback и повторный cleanup |
| README.md, config.dualstack.example.yaml | Контракты, миграция, реальные границы smoke-test |
| tools/build_archive.py | Включить новый публичный пример, продолжить исключать production configs |
| VERSION, THIRD_PARTY_NOTICES.md | 1.4.0; notices/pins менять только при фактическом изменении зависимости |

До релиза: offline tests; Linux runtime pinned binary для двух семей; регрессия
direct/SOCKS/ME/TLS-front; старые и новые ссылки в Telegram; публичные DNS A+AAAA;
внешний тест с IPv6 и IPv4-only клиентом; повторный setup без смены secrets;
rollback v2→v1; cleanup всех собственных правил без затрагивания соседа.
Проверить shell syntax, ZIP modes/состав/SHA и gitleaks. Секреты, ссылки и реальные
deployment hosts в репозиторий/архив не включать.

## Источники и ограничения исследования

Текущее поведение установщика проверено по локальным исходникам 1.3.2.
Нативные public_host, server.listeners/announce и network family options сверены
с документацией закреплённого Telemt 3.5.16:

- https://github.com/telemt/telemt/blob/3.5.16/config.toml
- https://github.com/telemt/telemt/blob/3.5.16/docs/Config_params/CONFIG_PARAMS.en.md

На момент составления плана socket semantics, network flags и импорт ссылок
клиентами ещё не были проверены. При реализации native WSL runtime подтвердил
dual-stack/IPv6-only sockets, API, каталог ссылок, TLS metadata и отсутствие
unscoped direct fallback при отказе SOCKS. Импорт ссылок и доставка сообщений
на реальных Telegram-клиентах остаются этапом dev-приёмки по DEV-1.4.0.md.
