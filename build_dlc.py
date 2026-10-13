#!/usr/bin/env python3
"""
hagezi/dns-blocklists  ->  v2fly domain-list-community (.dat)

Скрипт:
  1. Скачивает списки HaGeZi (wildcard/pro.txt, wildcard/pro.plus.txt, wildcard/ultimate.txt).
  2. Нормализует домены под формат dlc (wildcard `*.example.com` -> `example.com`,
     тип RootDomain в geosite и так матчит домен вместе со всеми поддоменами).
  3. Собирает по одному .dat на список с помощью бинарника `dlc`.
  4. Сравнивает результат с прошлым прогоном (state/hagezi-dlc.json) и, если .dat
     изменились, коммитит и пушит их в текущую ветку.

geosite-категории (cn / geolocation-!cn / private) намеренно НЕ добавляются:
в .dat попадает только блокировка hagezi.

Использование (локально):
    python3 scripts/build_dlc.py --repo-root . --dlc-bin bin/dlc --workdir /tmp/hagezi
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GITHUB_API = "https://api.github.com"
DLC_RELEASES_API = f"{GITHUB_API}/repos/v2fly/domain-list-community/releases/latest"

# HaGeZi обновляет списки примерно каждые 8 часов; зеркала для надёжности.
MIRRORS = [
    "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/{path}",
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/{path}",
    "https://fastly.jsdelivr.net/gh/hagezi/dns-blocklists@latest/{path}",
]

MAX_LINE = 4 * 1024 * 1024
MAX_DOMAIN_LEN = 253
MAX_LABEL_LEN = 63

# Имена файлов/списков валидны для dlc: [A-Z0-9!-].
# listname совпадает с именем .dat без расширения — так правило в конфиге ядра
# читается естественно: "ext:hagezi-pro.dat:hagezi-pro" / "geosite:hagezi-pro".
LISTS = [
    {
        "key": "pro",
        "title": "HaGeZi Multi PRO",
        "source": "wildcard/pro.txt",
        "listname": "hagezi-pro",
        "outfile": "hagezi-pro.dat",
    },
    {
        "key": "pro-plus",
        "title": "HaGeZi Multi PRO+",
        "source": "wildcard/pro.plus.txt",
        "listname": "hagezi-pro-plus",
        "outfile": "hagezi-pro-plus.dat",
    },
    {
        "key": "ultimate",
        "title": "HaGeZi Multi ULTIMATE",
        "source": "wildcard/ultimate.txt",
        "listname": "hagezi-ultimate",
        "outfile": "hagezi-ultimate.dat",
    },
]

VALID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-")


# --------------------------------------------------------------------------- #
# утилиты
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def gha(name: str, value: str) -> None:
    """Пишем выходное значение шага GitHub Actions."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log(f"$ {' '.join(shlex.quote(c) for c in cmd)}")
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


# --------------------------------------------------------------------------- #
# загрузка
# --------------------------------------------------------------------------- #
def http_get(url: str, timeout: int = 120, retries: int = 3) -> bytes:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "hagezi-dlc-github-action",
                    "Accept": "*/*",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_err = exc
            wait = 5 * attempt
            log(f"  [warn] попытка {attempt}/{retries} не удалась ({exc}); ждём {wait}c")
            time.sleep(wait)
    raise RuntimeError(f"не удалось скачать {url}: {last_err}")


def download_list(source_path: str, dest: Path, timeout: int = 120) -> tuple[str, int]:
    """Скачивает список, возвращает (url, размер в байтах)."""
    errors = []
    for mirror in MIRRORS:
        url = mirror.format(path=source_path)
        try:
            data = http_get(url, timeout=timeout, retries=2)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
            continue
        if not data or b"\n" not in data:
            errors.append(f"{url}: пустой/битый ответ ({len(data)} байт)")
            continue
        dest.write_bytes(data)
        return url, len(data)
    raise RuntimeError("все зеркала недоступны:\n  " + "\n  ".join(errors))


# --------------------------------------------------------------------------- #
# нормализация под формат dlc
# --------------------------------------------------------------------------- #
def valid_domain(domain: str) -> bool:
    if not domain or len(domain) > MAX_DOMAIN_LEN or any(c not in VALID_CHARS for c in domain):
        return False
    if domain.startswith(".") or domain.endswith("."):
        return False
    for label in domain.split("."):
        if (
            not label
            or len(label) > MAX_LABEL_LEN
            or label.startswith("-")
            or label.endswith("-")
            or "." not in domain  # одноуровневое "доменное имя" — почти всегда мусор
        ):
            return False
    return True


def normalize(src: Path, dst: Path) -> dict:
    """
    Пишет в dst по одному домену на строку (без префикса — dlc трактует как
    тип `domain`, он же RootDomain: матчит и сам домен, и все поддомены).
    """
    stats = {
        "raw_lines": 0,
        "comments": 0,
        "empty": 0,
        "wildcard": 0,
        "punycode": 0,
        "hosts_style": 0,
        "urls": 0,
        "invalid": 0,
        "duplicates": 0,
        "rules": 0,
        "version": "",
        "last_modified": "",
        "title": "",
        "samples": [],
    }
    seen: set[str] = set()
    out: list[str] = []

    with open(src, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stats["raw_lines"] += 1
            line = line.rstrip("\r\n")

            # комментарии и шапка списка (# Version / # Last modified / # Title ...)
            stripped = line.strip()
            if stripped.startswith("#"):
                stats["comments"] += 1
                low = stripped.lower()
                for meta_key, header in (
                    ("version", "# version:"),
                    ("last_modified", "# last modified:"),
                    ("title", "# title:"),
                ):
                    if low.startswith(header):
                        value = stripped[len(header):].strip()
                        if value and not stats[meta_key]:
                            stats[meta_key] = value
                continue

            # dlc сам режет всё после '#' — делаем так же (inline-комментарии)
            line = line.split("#", 1)[0].strip()
            if not line:
                stats["empty"] += 1
                continue

            # hosts-формат: 0.0.0.0 example.com / 127.0.0.1 example.com
            parts = line.split()
            if len(parts) >= 2 and parts[0] in {"0.0.0.0", "127.0.0.1", "::", "::1"}:
                line = parts[1]
                stats["hosts_style"] += 1
            elif len(parts) > 1:
                stats["invalid"] += 1
                if len(stats["samples"]) < 5:
                    stats["samples"].append(line)
                continue

            # dnsmasq-формат: local=/example.com/
            if line.startswith("local=/") and line.endswith("/"):
                line = line[len("local=/") : -1]

            # URL-формат: https://example.com/path
            if "://" in line:
                line = line.split("://", 1)[1]
                stats["urls"] += 1
            line = line.split("/", 1)[0].strip().rstrip(".").lower()

            # wildcard: *.example.com и *.example.com.  ->  example.com
            if line.startswith("*."):
                line = line[2:].strip().rstrip(".")
                stats["wildcard"] += 1
            elif line == "*":
                stats["invalid"] += 1
                continue

            # IDN / unicode -> punycode (dlc принимает только [a-z0-9.-])
            if not line.isascii():
                try:
                    line = line.encode("idna").decode("ascii")
                    stats["punycode"] += 1
                except (UnicodeError, UnicodeDecodeError):
                    stats["invalid"] += 1
                    if len(stats["samples"]) < 5:
                        stats["samples"].append(line)
                    continue

            if not valid_domain(line):
                stats["invalid"] += 1
                if len(stats["samples"]) < 5:
                    stats["samples"].append(line)
                continue

            if line in seen:
                stats["duplicates"] += 1
                continue
            seen.add(line)
            out.append(line)

    out.sort()
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + ("\n" if out else ""))

    stats["rules"] = len(out)
    return stats


# --------------------------------------------------------------------------- #
# dlc
# --------------------------------------------------------------------------- #
def default_dlc_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, "amd64")
    if system == "darwin":
        return f"dlc-darwin-{arch}"
    if system == "windows":
        return f"dlc-windows-{arch}.exe"
    return f"dlc-linux-{arch}"


def ensure_dlc(dlc_bin: Path, repo_root: Path, allow_build: bool) -> None:
    if dlc_bin.exists() and os.access(dlc_bin, os.X_OK):
        log(f"dlc найден: {dlc_bin}")
        return

    # 1) может, бинарник лежит рядом, но без +x (Windows-артефакт / свежий клон)
    if dlc_bin.exists():
        dlc_bin.chmod(0o755)
        log(f"выставлен +x для {dlc_bin}")
        return

    candidates = [repo_root / "bin" / default_dlc_name(), repo_root / "bin" / "dlc"]
    for cand in candidates:
        if cand.exists():
            cand.chmod(0o755)
            shutil.copy2(cand, dlc_bin)
            log(f"dlc взят из {cand}")
            return

    if not allow_build:
        raise RuntimeError(
            f"бинарник dlc не найден ({dlc_bin}).\n"
            "Положите его в репозиторий, например:\n"
            f"  mkdir -p bin && cp /path/to/dlc bin/dlc && chmod +x bin/dlc\n"
            "Или запустите с --allow-build (нужен Go) / соберите сами:\n"
            "  git clone --depth 1 https://github.com/v2fly/domain-list-community\n"
            "  cd domain-list-community && go build -trimpath -ldflags='-s -w' -o dlc ."
        )

    # 2) fallback: собрать из исходников
    if not shutil.which("go"):
        raise RuntimeError("dlc не найден, а Go не установлен — собрать бинарник нечем")
    log("dlc не найден — собираю из исходников v2fly/domain-list-community ...")
    src = Path("/tmp/domain-list-community")
    if src.exists():
        shutil.rmtree(src)
    run(["git", "clone", "--depth", "1", "https://github.com/v2fly/domain-list-community.git", str(src)])
    run(["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(dlc_bin), "."], cwd=src)
    dlc_bin.chmod(0o755)
    log(f"собрано: {dlc_bin}")


def dlc_version(dlc_bin: Path) -> str:
    try:
        data = json.loads(http_get(DLC_RELEASES_API, timeout=30, retries=2))
        return str(data.get("tag_name", "unknown"))
    except Exception:  # noqa: BLE001
        return "unknown"


def build_dat(dlc_bin: Path, workdir: Path, listname: str, outfile: str) -> Path:
    """
    Один прогон dlc на один список:
      datapath  — каталог с единственным файлом <LISTNAME>
      outputdir — каталог, куда ляжет <outfile>
    """
    data_dir = workdir / "data" / listname.lower()
    out_dir = workdir / "out" / listname.lower()
    shutil.rmtree(data_dir, ignore_errors=True)
    shutil.rmtree(out_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(workdir / "rules" / f"{listname.lower()}.txt", data_dir / listname)

    run(
        [
            str(dlc_bin),
            "-datapath", str(data_dir),
            "-outputdir", str(out_dir),
            "-outputname", outfile,
        ]
    )
    produced = out_dir / outfile
    if not produced.exists():
        raise RuntimeError(f"dlc не создал {produced}")
    return produced


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
def git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=True, check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {proc.returncode}\n{proc.stderr.strip()}")
    return proc


def git_quiet(repo_root: Path, *args: str) -> bool:
    """Возвращает True, если команда завершилась успешно."""
    return git(repo_root, *args, check=False).returncode == 0


def commit_and_push(repo_root: Path, message: str, branch: str, do_push: bool) -> bool:
    """Коммитит все изменения (возвращает True, если коммит создан) и пушит."""
    git(repo_root, "add", "--all")
    if git_quiet(repo_root, "diff", "--cached", "--quiet"):
        log("изменений для коммита нет")
        return False

    log(f"коммичу:\n{message}")
    git(repo_root, "-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", message)

    if not do_push:
        log("пуш пропущен (не задан --push / нет origin)")
        return True

    has_remote = bool(git(repo_root, "remote", "get-url", "origin", check=False).stdout.strip())
    if not has_remote:
        log("[warn] remote 'origin' не настроен — коммит остался локально")
        return True

    for attempt in range(1, 4):
        git(repo_root, "pull", "--rebase", "--autostash", "origin", branch, check=False)
        pushed = git(repo_root, "push", "origin", f"HEAD:{branch}", check=False)
        if pushed.returncode == 0:
            log("изменения запушены")
            return True
        log(f"[warn] push не удался (попытка {attempt}/3): {pushed.stderr.strip()}")
        time.sleep(5 * attempt)
    raise RuntimeError("не удалось запушить изменения после 3 попыток")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Сборка .dat (v2fly DLC) из списков HaGeZi")
    ap.add_argument("--repo-root", default=".", help="корень git-репозитория, куда пушим .dat")
    ap.add_argument("--dlc-bin", default=None, help="путь к бинарнику dlc (по умолчанию <repo-root>/bin/dlc)")
    ap.add_argument("--workdir", default=None, help="временный каталог (по умолчанию создаётся сам)")
    ap.add_argument("--out-dir", default=None, help="куда класть .dat (по умолчанию <repo-root>/dat)")
    ap.add_argument("--state-file", default=None, help="файл состояния (по умолчанию <repo-root>/state/hagezi-dlc.json)")
    ap.add_argument("--lists", default=None, help="список ключей через запятую: pro,pro-plus,ultimate")
    ap.add_argument("--listname-prefix", default="", help="префикс имени geosite-листа (по умолчанию пусто, имя = имя файла)")
    ap.add_argument("--allow-build", action="store_true", help="если dlc нет — собрать его через go build")
    ap.add_argument("--no-state-update", action="store_true", help="не обновлять state-файл (коммиты только при изменении .dat)")
    ap.add_argument("--commit", action="store_true", help="коммитить изменения")
    ap.add_argument("--push", action="store_true", help="пушить в origin (нужен --commit)")
    ap.add_argument("--branch", default=None, help="ветка для пуша (по умолчанию текущая)")
    ap.add_argument("--timeout", type=int, default=120, help="таймаут HTTP, секунд")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    dlc_bin = Path(args.dlc_bin).resolve() if args.dlc_bin else repo_root / "bin" / "dlc"
    out_dir = Path(args.out_dir).resolve() if args.out_dir else repo_root / "dat"
    state_file = Path(args.state_file).resolve() if args.state_file else repo_root / "state" / "hagezi-dlc.json"
    workdir = Path(args.workdir).resolve() if args.workdir else Path("/tmp/hagezi-dlc-work")

    selected = LISTS
    if args.lists:
        keys = {k.strip() for k in args.lists.split(",") if k.strip()}
        selected = [l for l in LISTS if l["key"] in keys]
        if not selected:
            log(f"[error] неизвестные ключи списков: {sorted(keys)}")
            return 2
    prefix = args.listname_prefix.upper()

    branch = args.branch or os.environ.get("GITHUB_REF_NAME")
    if not branch:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        branch = proc.stdout.strip() if proc.returncode == 0 else ""
    if not branch or branch == "HEAD":
        branch = "main"  # свежий репозиторий без коммитов / detached HEAD

    log(f"repo root : {repo_root}")
    log(f"branch    : {branch}")
    log(f"dlc bin   : {dlc_bin}")
    log(f"out dir   : {out_dir}")
    log(f"workdir   : {workdir}")

    shutil.rmtree(workdir, ignore_errors=True)
    (workdir / "raw").mkdir(parents=True, exist_ok=True)
    (workdir / "rules").mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    ensure_dlc(dlc_bin, repo_root, args.allow_build)

    old_state: dict = {}
    if state_file.exists():
        try:
            old_state = json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("[warn] state-файл повреждён, пересобираю всё")

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_state = {
        "generated_at": now_utc,
        "checked_at": now_utc,
        "dlc_release": dlc_version(dlc_bin),
        "lists": {},
    }

    changed: list[str] = []
    summary_rows = ["| Список | Версия HaGeZi | Правил | Размер .dat | Изменился |", "|---|---|---|---|---|"]

    for item in selected:
        key, source = item["key"], item["source"]
        listname = f"{prefix}{item['listname']}".upper()
        outfile = item["outfile"]
        log("")
        log(f"=== {item['title']} ({source}) -> {outfile} [{listname}] ===")

        raw = workdir / "raw" / Path(source).name
        url, size = download_list(source, raw, timeout=args.timeout)
        log(f"скачано: {url} ({size:,} байт)")
        src_sha = sha256_file(raw)

        rules = workdir / "rules" / f"{listname.lower()}.txt"
        stats = normalize(raw, rules)
        log(
            f"нормализовано: правил={stats['rules']:,}, wildcard={stats['wildcard']:,}, "
            f"дубли={stats['duplicates']:,}, отброшено={stats['invalid']:,}"
        )
        if stats["samples"]:
            log(f"  примеры отброшенного: {stats['samples']}")
        if stats["rules"] < 1000:
            raise RuntimeError(
                f"{source}: подозрительно мало правил ({stats['rules']}) — похоже, список битый"
            )

        dat_src = build_dat(dlc_bin, workdir, listname, outfile)
        dat_dst = out_dir / outfile
        new_sha = sha256_file(dat_src)
        old_sha = (old_state.get("lists", {}).get(key) or {}).get("dat_sha256")
        is_changed = new_sha != old_sha

        shutil.copy2(dat_src, dat_dst)

        new_state["lists"][key] = {
            "title": stats["title"] or item["title"],
            "source": source,
            "source_url": url,
            "hagezi_version": stats["version"],
            "hagezi_last_modified": stats["last_modified"],
            "source_sha256": src_sha,
            "rules": stats["rules"],
            "listname": listname,
            "outfile": outfile,
            "dat_sha256": new_sha,
            "dat_size": dat_dst.stat().st_size,
            "changed": is_changed,
        }
        if is_changed:
            changed.append(key)

        summary_rows.append(
            f"| {item['title']} | {stats['version'] or '—'} | {stats['rules']:,} | "
            f"{dat_dst.stat().st_size / 1024 / 1024:.2f} MiB | {'✅ да' if is_changed else '— нет'} |"
        )
        log(f"готово: {dat_dst} ({dat_dst.stat().st_size:,} байт), изменился={is_changed}")

    # ---- state-файл и итог -------------------------------------------------
    lists_changed = (old_state.get("lists") or {}) != new_state["lists"]
    if not lists_changed and old_state.get("checked_at"):
        # содержимое то же самое — сохраняем старую метку времени,
        # иначе state-файл менялся бы каждый прогон и давал пустые коммиты
        new_state["checked_at"] = old_state["checked_at"]
        new_state["generated_at"] = old_state.get("generated_at", now_utc)

    new_state_text = json.dumps(new_state, indent=2, ensure_ascii=False) + "\n"
    state_changed = (not state_file.exists()) or state_file.read_text(encoding="utf-8") != new_state_text
    if state_changed and not args.no_state_update:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(new_state_text, encoding="utf-8")
        log(f"state-файл обновлён: {state_file}")
    elif args.no_state_update and state_changed:
        log("--no-state-update: state-файл не трогаю")

    versions = ", ".join(
        f"{k}={new_state['lists'][k]['hagezi_version'] or '?'}" for k in new_state["lists"]
    )
    log("")
    log(f"изменилось списков: {len(changed)} {changed or ''}")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("## HaGeZi → DLC .dat\n\n")
            fh.write("\n".join(summary_rows) + "\n\n")
            fh.write(f"- Версии источников: `{versions}`\n")
            fh.write(f"- dlc release: `{new_state['dlc_release']}`\n")
            fh.write(f"- Изменения: **{'есть (' + ', '.join(changed) + ')' if changed else 'нет'}**\n")

    gha("versions", versions)
    gha("changed", "true" if changed else "false")
    gha("changed_lists", ",".join(changed))
    gha("dat_sha256", sha256_text("".join(new_state["lists"][k]["dat_sha256"] for k in sorted(new_state["lists"]))))

    if not changed:
        log("все .dat идентичны прошлой сборке — содержимое не менялось")
    if not args.commit:
        log("--commit не указан: файлы оставлены как есть, ничего не коммичу")
        return 0

    if not changed and not state_changed:
        log("ничего не изменилось — коммит не создаётся (state-файл не трогали)")
        gha("committed", "false")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if changed:
        message = (
            f"chore(dat): update HaGeZi blocklists [{stamp}]\n\n"
            f"Изменились списки: {', '.join(changed)}\n"
            f"Версии HaGeZi: {versions}\n"
            f"Правил: "
            + ", ".join(f"{k}={new_state['lists'][k]['rules']:,}" for k in sorted(new_state["lists"]))
            + f"\nСобрано: v2fly/domain-list-community {new_state['dlc_release']}\n"
            f"Источник: https://github.com/hagezi/dns-blocklists"
        )
    else:
        # .dat те же, но поменялся state (например, обновился dlc) — фиксируем это
        message = f"chore(state): refresh HaGeZi build state [{stamp}]\n\nВерсии HaGeZi: {versions}"

    committed = commit_and_push(repo_root, message, branch, args.push)
    gha("committed", "true" if committed else "false")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"[FATAL] {exc}")
        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if step_summary:
            with open(step_summary, "a", encoding="utf-8") as fh:
                fh.write(f"\n### ❌ Ошибка\n\n```\n{exc}\n```\n")
        sys.exit(1)
