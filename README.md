# HaGeZi DNS blocklists → v2fly DLC `.dat`

GitHub Action, который раз в 3 дня скачивает списки [HaGeZi](https://github.com/hagezi/dns-blocklists)
(**PRO**, **PRO+**, **ULTIMATE**), конвертирует их в geosite-формат `.dat`
с помощью [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community) (`dlc`)
и пушит результат в этот репозиторий — **только если содержимое `.dat` реально изменилось**.

## Что получается на выходе

| Файл | Список HaGeZi | Имя листа внутри `.dat` | Правил |
|---|---|---|---|
| `dat/hagezi-pro.dat` | [Multi PRO](https://github.com/hagezi/dns-blocklists#pro) | `hagezi-pro` | ~194 000 |
| `dat/hagezi-pro-plus.dat` | [Multi PRO+](https://github.com/hagezi/dns-blocklists#proplus) | `hagezi-pro-plus` | ~208 000 |
| `dat/hagezi-ultimate.dat` | [Multi ULTIMATE](https://github.com/hagezi/dns-blocklists#ultimate) | `hagezi-ultimate` | ~231 000 |

Каждый `.dat` — отдельный файл, списки не смешиваются. Стандартные geosite-категории
(`cn`, `geolocation-!cn`, `private`) **не добавляются**, поэтому эти файлы нельзя
использовать как замену `geosite.dat`/`dlc.dat` — только как дополнительный geodata-файл.

Имя листа совпадает с именем файла без расширения, поэтому правило в конфиге читается естественно:
`ext:hagezi-pro-plus.dat:hagezi-pro-plus`.

## Структура репозитория

```
.github/workflows/update-hagezi-dat.yml   # Action: cron раз в 3 дня + ручной запуск
scripts/build_dlc.py                      # скачивание → нормализация → сборка dlc → git push
bin/dlc                                   # go-бинарник dlc (кладёте сами, ~4 MiB)
dat/*.dat                                 # результат — их подключает v2ray/xray
state/hagezi-dlc.json                     # sha256 прошлой сборки + версии источников (анти-шум коммитов)
.gitattributes                            # чтобы GitHub не пытался диффить бинарные .dat
```

## Установка

### 1. Положите бинарник `dlc` в `bin/dlc`

Готовых бинарников `dlc` в релизах v2fly нет (там только `dlc.dat`), поэтому собираем из исходников:

```bash
git clone --depth 1 https://github.com/v2fly/domain-list-community
cd domain-list-community
go build -trimpath -ldflags='-s -w' -o dlc .     # нужен Go 1.25+ (версия из go.mod)
cd ..
mkdir -p bin && mv domain-list-community/dlc bin/dlc && chmod +x bin/dlc
git add bin/dlc && git commit -m "add dlc binary"
```

> **Не хотите хранить бинарник в репозитории?** Запустите workflow с input `allow_build=true` —
> Action соберёт `dlc` сам через `actions/setup-go` (+30–60 секунд к времени работы).
> Чтобы это стало поведением по умолчанию, поменяйте в workflow
> `default: false` → `default: true` у `inputs.allow_build`.

### 2. Скопируйте в свой репозиторий

- `.github/workflows/update-hagezi-dat.yml`
- `scripts/build_dlc.py`
- `.gitattributes`

### 3. Дайте права на запись

*Settings → Actions → General → Workflow permissions → Read and write permissions*
(в файле workflow уже объявлен `permissions: contents: write`).

### 4. Первый запуск

Вкладка *Actions* → *Update HaGeZi .dat lists* → *Run workflow*.
Первый прогон (~30 секунд) создаст `dat/*.dat` и `state/hagezi-dlc.json` одним коммитом
от `github-actions[bot]`. Дальше всё будет происходить по расписанию.

## Подключение в v2ray-core v5 / Xray

Оба ядра читают geodata-файлы **локально** из asset-каталога
(env `v2ray.location.asset` / `xray.location.asset`, по умолчанию — каталог рядом с бинарником ядра).
Скачать `.dat` на сервер нужно отдельно, например:

```bash
ASSET_DIR=/usr/local/share/v2ray      # или $XRAY_LOCATION_ASSET
mkdir -p "$ASSET_DIR"
curl -L -o "$ASSET_DIR/hagezi-pro-plus.dat" \
  https://github.com/<USER>/<REPO>/raw/main/dat/hagezi-pro-plus.dat
```

### Правила маршрутизации

**Xray** (поддерживаются префиксы `ext:`, `ext-domain:`, `ext-site:`):

```jsonc
{
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [
      {
        "type": "field",
        "outboundTag": "blocked",
        "domain": ["ext-domain:hagezi-pro-plus.dat:hagezi-pro-plus"]
      }
    ]
  },
  "outbounds": [
    { "tag": "blocked", "protocol": "blackhole" }
  ]
}
```

**v2ray-core v5** (префиксы `ext:` / `ext-domain:`):

```jsonc
{
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "geodataMode": true,
    "geodataLoader": "memconservative",
    "rules": [
      {
        "type": "field",
        "outboundTag": "blocked",
        "domain": ["ext:hagezi-ultimate.dat:hagezi-ultimate"]
      }
    ]
  }
}
```

### Автообновление `.dat` на сервере

**Вариант A — встроенный в Xray загрузчик geodata** (секция верхнего уровня; файл уже должен лежать
в asset-каталоге, Xray будет его перезакачивать по cron):

```jsonc
{
  "geodata": {
    "cron": "@daily",
    "outbound": "direct",
    "assets": [
      {
        "url": "https://github.com/<USER>/<REPO>/raw/main/dat/hagezi-pro-plus.dat",
        "file": "hagezi-pro-plus.dat"
      }
    ]
  }
}
```

**Вариант B — свой cron/systemd-timer** с тем же `curl`, что выше. Подходит для v2ray-core v5
(он сам geodata из сети не качает).

### Если нужен один общий файл с geosite-категориями

`geosite:hagezi-pro-plus` сработает только если лист лежит внутри файла `geosite.dat` (`dlc.dat`).
Тогда берёте официальный [`dlc.dat`](https://github.com/v2fly/domain-list-community/releases/latest),
распаковываете его списки, добавляете hagezi и собираете всё одним прогоном `dlc`:

```bash
mkdir -p data && cd data
/tmp/datdump -inputdata /path/to/dlc.dat -outputdir . -exportlists ''   # распакует все листы
mv hagezi-pro-plus.dat_plain.yml ../   # (формат yml — только для проверки, в data/ кладём txt)
cd ..
# нормализованный список hagezi (см. --workdir .../rules/hagezi-pro-plus.txt) копируем как имя листа:
cp /tmp/hagezi-work/rules/hagezi-pro-plus.txt data/hagezi-pro-plus
./bin/dlc -datapath ./data -outputdir ./out -outputname geosite.dat
# правило: "domain": ["geosite:hagezi-pro-plus"]
```

## Расписание: «каждые 3 дня»

```yaml
on:
  schedule:
    - cron: "17 4 */3 * *"     # 1, 4, 7, 10, ... 31 число, 04:17 UTC
  workflow_dispatch: { ... }   # ручной запуск с параметрами
```

Особенности cron в GitHub Actions, о которых стоит знать:

- `*/3` в поле «день месяца» = 1, 4, 7, …, 28, 31 → на стыке месяцев интервал может быть 1–3 дня.
  «Ровно каждые 72 часа» cron в GitHub не умеет.
- Запуск может задержаться на 5–60 минут (очередь раннеров), при высокой нагрузке — дольше.
- **Scheduled-воркфлоу работают только из ветки по умолчанию** (`main`/`master`).
  Если файл лежит в другой ветке — по cron он не запустится.
- В неактивных 60 дней репозиториях GitHub отключает расписание (лечится любым коммитом в файл workflow).

## Защита от лишних коммитов

HaGeZi обновляет списки примерно каждые 8 часов, но «источник обновился» ≠ «изменился `.dat`».
Скрипт сравнивает **sha256 собранного `.dat`** с записью в `state/hagezi-dlc.json`:

- `.dat` не изменился → коммита нет вовсе (в логе: `ничего не изменилось — коммит не создаётся`);
- изменился хотя бы один `.dat` → один коммит со всеми файлами и сообщением вида:

  ```
  chore(dat): update HaGeZi blocklists [2026-10-13 16:48 UTC]

  Изменились списки: pro
  Версии HaGeZi: pro=2026.1013.0830.16, pro-plus=2026.1013.0838.14, ultimate=2026.1013.0842.29
  Правил: pro=194,332, pro-plus=208,012, ultimate=231,532
  Собрано: v2fly/domain-list-community 20261009101206
  ```

Дополнительно:

- `concurrency: group: hagezi-dlc-update` — параллельные запуски не пересекаются;
- перед пушем `git pull --rebase --autostash`, при неудаче — до 3 попыток;
- если в скачанном списке меньше 1000 правил — сборка падает (защита от битого/пустого зеркала);
- зеркала по порядку: `cdn.jsdelivr.net` → `raw.githubusercontent.com` → `fastly.jsdelivr.net`;
- в `state/hagezi-dlc.json` хранятся версия HaGeZi, sha256 источника, sha256 `.dat` и метка последнего
  реального изменения (`checked_at` обновляется только когда что-то поменялось — иначе файл не переписывается);

## Настройка

Флаги `scripts/build_dlc.py`:

| Флаг | По умолчанию | Что делает |
|---|---|---|
| `--lists` | `pro,pro-plus,ultimate` | какие списки собирать |
| `--out-dir` | `dat` | каталог для `.dat` в репозитории |
| `--dlc-bin` | `<repo>/bin/dlc` | путь к бинарнику `dlc` |
| `--listname-prefix` | пусто | префикс имени листа внутри `.dat` |
| `--allow-build` | выкл | собрать `dlc` через `go build`, если бинарника нет |
| `--no-state-update` | выкл | не трогать state-файл (коммиты строго только при изменении `.dat`) |
| `--commit` / `--push` | выкл | коммитить / пушить (в workflow оба включены) |
| `--branch` | текущая / `$GITHUB_REF_NAME` | куда пушить |
| `--workdir`, `--state-file`, `--timeout` | — | служебное |

Input'ы workflow: `lists`, `allow_build`, `force` (удалить state и пересобрать всё заново).

Пример — собрать только ULTIMATE:

```bash
python3 scripts/build_dlc.py --repo-root . --lists ultimate
```

## Локальный запуск (без GitHub Actions)

```bash
python3 scripts/build_dlc.py \
  --repo-root . \
  --dlc-bin bin/dlc \
  --out-dir dat \
  --state-file state/hagezi-dlc.json \
  --workdir /tmp/hagezi-work \
  --commit --push --branch main
```

Без `--commit`/`--push` скрипт просто скачает, сконвертирует и положит `.dat` в `--out-dir`.
Нужен только Python 3.9+ (сторонние пакеты не требуются).

## Проверка результата

Утилита `datdump` из репозитория v2fly вытаскивает списки обратно из `.dat`:

```bash
cd domain-list-community && go build -o /tmp/datdump ./cmd/datdump
/tmp/datdump -inputdata dat/hagezi-pro.dat -outputdir /tmp/dump
head -8 /tmp/dump/hagezi-pro.dat_plain.yml
```

```yaml
lists:
  - name: "hagezi-pro"
    length: 194332
    rules:
      - "domain:0--4.com"
      - "domain:0--d.com"
      ...
```

## Технические детали конвертации

- Берутся файлы формата **wildcard** (`wildcard/pro.txt`, `wildcard/pro.plus.txt`, `wildcard/ultimate.txt`),
  где каждая строка — `*.example.com`.
- В geosite/protobuf **типа wildcard нет**, а тип `domain` (`Domain_RootDomain`) и так матчит домен
  вместе со всеми поддоменами. Поэтому `*.example.com` → `domain:example.com`.
  Единственное отличие от оригинала: голый apex `example.com` тоже попадёт под блокировку —
  в списках HaGeZi apex-домены и так перечислены отдельными строками, так что потери минимальны.
  Если нужно строго «только поддомены», поменяйте тип на `full:` в `normalize()`
  (но тогда поддомены блокироваться не будут — обычно это не то, что нужно).
- `dlc` валидирует каждую строку и **падает на первой некорректной**, поэтому скрипт приводит список
  к его формату: lowercase, удаление `#`-комментариев (в т.ч. inline), разбор hosts/dnsmasq/URL-форматов,
  IDN → punycode, отсев строк вне `[a-z0-9.-]`, проверка длины домена/меток, дедупликация, сортировка.
  На текущих списках HaGeZi отброшенных строк: 0.
- `dlc` дополнительно вычищает избыточные поддомены (если есть `example.com`, строка `a.example.com` удаляется),
  поэтому число правил в `.dat` может быть чуть меньше числа строк в источнике.
- **IP-адреса** из списков HaGeZi в geosite не попадают: geosite-формат работает только с доменами
  (`ips/` и «Normal»-версии с `0.0.0.0 host` для этого не годятся). Для IP нужен отдельный `geoip.dat`.
- Размер `.dat`: pro ≈ 4.4 MiB, pro-plus ≈ 4.8 MiB, ultimate ≈ 5.3 MiB — в GitHub-репозиторий влезают свободно.

## Лицензии

- Списки HaGeZi: [CC BY-SA 4.0](https://github.com/hagezi/dns-blocklists/blob/main/LICENSE) — при распространении указывайте авторство.
- `dlc` (v2fly/domain-list-community): MIT.
