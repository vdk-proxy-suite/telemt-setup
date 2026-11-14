# Приёмка telemt-setup 1.4.0 на отдельных dev-инстансах

Ниже новые инстансы, новые secrets и новые порты. Production Telemt и 3proxy
не останавливаются и не обновляются. Генератор не читает production secrets.
Одна распаковка на каждый ID. Команды VM выполнять от обычного пользователя
с sudo, блоками, прекращая работу при ошибке.

## Матрица

| ID | Proxy TCP | API (localhost) | Metrics (localhost) | Маршрут |
| --- | --- | --- | --- | --- |
| dev140-v4 | 21110 | 29100 | 29200 | Direct, вход IPv4 |
| dev140-v6 | 21111 | 29101 | 29201 | Direct, вход IPv6 |
| dev140-dual | 21112 | 29102 | 29202 | Direct, DNS/IPv4/IPv6 |
| dev140-me | 21113 | 29103 | 29203 | Direct + Middle Proxy, DNS/IPv4/IPv6 |
| dev140-via-direct | 21114 | 29104 | 29204 | SOCKS → 127.0.0.1:11080 → direct |
| dev140-via-socks | 21115 | 29105 | 29205 | SOCKS → 127.0.0.1:11081 → SOCKS parent |
| dev140-via-http | 21116 | 29106 | 29206 | SOCKS → 127.0.0.1:11082 → HTTP parent |
| dev140-via-https | 21117 | 29107 | 29207 | SOCKS → 127.0.0.1:11083 → HTTPS parent |

Исходящий Telegram-трафик всех профилей по умолчанию IPv4. Входящий IPv6
тестируется независимо. Middle Proxy выключен у четырёх цепочек через local
3proxy. Если ваши локальные SOCKS-порты другие, задайте генератору --socks-base
или отредактируйте конкретные upstream.address перед установкой.

## Подготовка на VM

Сначала проверьте `sudo ss -lntp`: диапазоны должны быть свободны. Installer
дополнительно проверяет зарегистрированные остановленные инстансы; он откажет
при конфликте. При необходимости используйте --proxy-base/--api-base/--metrics-base.
DNS должен иметь A и AAAA этой VM. В cloud firewall разрешите TCP21110–21117
для тестовых клиентов; API/metrics наружу не открывайте. UFW должен быть включён
для IPv6 заранее; setup не меняет его глобальную политику.

```bash
umask 077
mkdir ~/telemt-140-dev
cd ~/telemt-140-dev
curl -fLO https://github.com/vdk-proxy-suite/telemt-setup/releases/download/v1.4.0/telemt-setup-standalone-1.4.0.zip
curl -fLO https://github.com/vdk-proxy-suite/telemt-setup/releases/download/v1.4.0/telemt-setup-standalone-1.4.0.zip.sha256
sha256sum -c telemt-setup-standalone-1.4.0.zip.sha256
unzip -q telemt-setup-standalone-1.4.0.zip -d controller
```

Замените документационные IP и домен на свои:

```bash
python3 controller/telemt-setup/tools/dev_profiles.py \
  --public-ipv4 192.0.2.10 --public-ipv6 2001:db8::10 \
  --domain proxy.example.org --output profiles
```

Генератор создаёт 8 файлов `profiles/config.dev140-*.yaml`. Проверьте домен,
upstreams и tls_domain. Файлы содержат `GENERATE`; реальный secret создаётся
только на VM при setup и сохраняется при повторном запуске того же ID.

## Развёртывание

Сначала только direct dual-stack:

```bash
(
set -e
cd ~/telemt-140-dev
mkdir dev140-dual
unzip -q telemt-setup-standalone-1.4.0.zip -d dev140-dual
chmod go-w dev140-dual dev140-dual/telemt-setup
chmod u+x dev140-dual/telemt-setup/setuptelemt.sh
sudo ./dev140-dual/telemt-setup/setuptelemt.sh all \
  --config "$PWD/profiles/config.dev140-dual.yaml"
)
```

После его успешной проверки повторите для остальных ID (не запускайте
параллельно). Блок остановится на первом неуспешном setup:

```bash
(
set -e
cd ~/telemt-140-dev
for id in dev140-v4 dev140-v6 dev140-me dev140-via-direct dev140-via-socks dev140-via-http dev140-via-https; do
  mkdir "$id"
  unzip -q telemt-setup-standalone-1.4.0.zip -d "$id"
  chmod go-w "$id" "$id/telemt-setup"
  chmod u+x "$id/telemt-setup/setuptelemt.sh"
  sudo "./$id/telemt-setup/setuptelemt.sh" all --config "$PWD/profiles/config.$id.yaml"
done
)
```

Если setup остановился, сохраните вывод. Для повторного запуска уже распакованного
ID повторите только его команду setup, а не mkdir/unzip. Не удаляйте marker и
не используйте каталог соседа. Не повторяйте all на production ID.

## Проверки на VM и клиентах

Для каждого успешно установленного ID:

```bash
sudo /usr/local/lib/telemt-setup/instances/dev140-dual/setuptelemt.sh healthcheck --instance dev140-dual
sudo /usr/local/lib/telemt-setup/instances/dev140-dual/setuptelemt.sh links --instance dev140-dual --format text
```

Проверить Telegram отдельно по DNS, IPv4 и IPv6 ссылкам одного пользователя:
подключение, отправка/получение сообщения, загрузка медиа. Ссылки содержат
credentials — не публикуйте вывод. Смена варианта endpoint не меняет secret.
В dev140-me проверить тот же сценарий; runtime readiness не доказывает доставку.

На Windows распакуйте тот же релиз и скопируйте с VM только YAML profiles
(они содержат GENERATE, без активных secrets). Затем, например:

```powershell
python tools/healthcheck.py --scope e2e --config profiles/config.dev140-dual.yaml --timeout 8 --json dev140-dual-vpn.json
python tools/healthcheck.py --scope e2e --config profiles/config.dev140-dual.yaml --family ipv4 --timeout 8 --json dev140-dual-ipv4.json
```

Первый запуск — через VPN с рабочим IPv6 и без исключений для Python, домена и
обоих IP VM. Второй можно выполнить без VPN на IPv4-only подключении. Каждый
JSON путь должен быть новым. Затем повторить полный E2E для остальных профилей.
`--family ipv4` явно пропускает IPv6 и не является подтверждением обеих семей.
TLS smoke не проверяет пользовательский secret/MTProto auth; это делает Telegram.

Дополнительные проверки до migration:

1. Для dev140-dual сохранить links локально, выполнить reconfigure с тем же
   YAML и убедиться, что secrets и ссылки не сменились
2. В отдельной копии его YAML заменить primary dns на ipv4; после reconfigure
   старые DNS/IPv4/IPv6 ссылки должны продолжать подключаться
3. Для проверки отсутствия fallback только у dev-профиля временно изменить
   SOCKS upstream.address на свободный закрытый localhost-порт. Reconfigure
   должен не пройти readiness и откатить предыдущее рабочее состояние; не
   останавливайте production local 3proxy ради этого теста
4. Проверить явный rollback после успешной смены primary и повторно проверить
   links, active state и firewall

```bash
sudo /usr/local/lib/telemt-setup/instances/dev140-dual/setuptelemt.sh rollback --instance dev140-dual
```

Для отдельного теста IPv6 egress в dev140-dual можно явно изменить outbound
на `{families: [ipv4, ipv6], prefer: ipv6}`; это требует IPv6-доступа VM к
Telegram и отдельной клиентской проверки. Не переносите эту настройку в профили
IPv4-only SOCKS parent. Успех входящего IPv6 сам по себе этого не требует.

## Очистка только dev

Сначала dry-run каждого успешно зарегистрированного dev-ID:

```bash
sudo /usr/local/lib/telemt-setup/instances/dev140-dual/cleantelemt.sh \
  --instance dev140-dual --purge-logs --purge-ufw --purge-setup
```

После проверки плана повторите с `--yes`. Затем так же остальные dev-ID.
Не используйте main/local или ID боевых Telemt. Cleaner удаляет принадлежащие
инстансу ресурсы и правила; manifest/controller сохраняются для повторной
очистки. Проверить `sudo ufw show added`, `sudo ss -lntp` и состояние production.
Архив, внешний каталог profiles и общий systemd journal cleaner не удаляет.
