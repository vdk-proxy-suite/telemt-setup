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
unzip telemt-setup-standalone-1.1.1.zip
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

## Firewall и эксплуатация

Для production-развёртывания откройте в cloud security group только входящий
TCP/443 и отдельно нужный диапазон для SSH. Telemt не использует UDP. API
`127.0.0.1:9091` и Prometheus `127.0.0.1:9090` не должны публиковаться наружу.

Установщик не меняет cloud firewall и по умолчанию не меняет UFW.
`install.manage_ufw: true` добавляет локальное allow-правило для proxy TCP-порта.
Не забудьте разрешить исходящий TCP к инфраструктуре Telegram и GitHub во время
установки.

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
готовность Telemt и Middle Proxy, но без авторизованного Telegram-клиента тест
не доказывает доставку сообщений.

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
- Лицензия: <https://github.com/telemt/telemt/blob/main/LICENSE>
