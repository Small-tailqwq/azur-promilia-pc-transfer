import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.request import urlopen


MANIFEST_SCHEMA = 1
RESOURCE_ROOT = "AzurPromilia_Data/StreamingAssets/.res/"
INVALID_COMPONENT = re.compile(r'[<>:"\\|?*\x00-\x1f]')


ROOT = Path(__file__).resolve().parent
GAME_EXE = "AzurPromilia.exe"
CHANNEL_CONFIG = "mj_sdk/res/app_config.dat"
INDEX_VERSION = "_index/version"
RELEASE_VERSION = "1.0.0.30"
VERSION_MARKER = b'{"version":"1.0.0.30","versionName":"1.0.0"}'
TARGETS = {"official": "官方 PC", "bilibili": "B 站"}


class UserError(ValueError):
    pass


@dataclass
class Prepared:
    path: Path
    linked_bytes: int
    copied_bytes: int
    missing_files: int
    missing_bytes: int


def plain_path(path, metadata):
    if (
        stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError(f"Reparse point: {path}")


def walk_error(error):
    raise error


def snapshot(root):
    files = {}
    for folder, dirs, names in os.walk(root, onerror=walk_error):
        for name in dirs:
            path = Path(folder, name)
            plain_path(path, path.lstat())
        for name in names:
            path = Path(folder, name)
            metadata = path.lstat()
            plain_path(path, metadata)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"Not a regular file: {path}")
            files[path.relative_to(root).as_posix()] = (
                metadata.st_size,
                metadata.st_mtime_ns,
            )
    return files


def fingerprint(path):
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"File changed while hashing: {path}")
    return [after.st_size, digest]


def directory(path):
    result = path.resolve(strict=True)
    if not result.is_dir():
        raise ValueError(f"Not a directory: {path}")
    return result


def load_manifest(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    if data["schema"] != MANIFEST_SCHEMA or set(data["channels"]) != set(TARGETS):
        raise ValueError("Unsupported manifest.")
    for entries in data["channels"].values():
        seen = set()
        for name in entries:
            key = name.casefold()
            if key in seen or any(
                p in ("", ".", "..")
                or p.endswith((".", " "))
                or INVALID_COMPONENT.search(p)
                or PureWindowsPath(p).is_reserved()
                for p in name.split("/")
            ):
                raise ValueError(f"Invalid manifest path: {name!r}")
            seen.add(key)
    return data["channels"]


def fresh_output(path, inputs):
    parent = directory(path.parent)
    output = parent / path.name
    if os.path.lexists(output) or any(output.is_relative_to(root) for root in inputs):
        raise ValueError(f"Output must be a new directory outside the inputs: {output}")
    return output


def find_game(path):
    for root in (path, path / "AzurPromilia_Game"):
        if (root / GAME_EXE).is_file():
            return root
    raise UserError("请选择 AzurPromilia.exe 所在的游戏目录。")


def matches(data, expected):
    return [len(data), hashlib.sha256(data).hexdigest()] == expected


def channel_config(source, target, expected, release_dir):
    existing = source / CHANNEL_CONFIG
    if existing.is_file() and existing.stat().st_size == expected[0]:
        data = existing.read_bytes()
        if matches(data, expected):
            return data
    urls = json.loads((release_dir / "repair-sources.json").read_text(encoding="utf-8"))
    size = expected[0]
    try:
        with urlopen(urls[target], timeout=15) as response:
            data = response.read(size + 1)
    except OSError as error:
        raise UserError("无法从发行方取得目标渠道的定位配置。请检查网络后重试。") from error
    if not matches(data, expected):
        raise UserError("发行方返回的定位配置与此版工具不匹配。")
    return data


def prepare(source_path, target, output_path, *, hardlink=True, verify_hashes=False,
            progress=None, release_dir=ROOT):
    if target not in TARGETS:
        raise ValueError(f"Unknown target channel: {target}")
    notify = progress or (lambda *_: None)
    source = directory(find_game(source_path))
    output = fresh_output(output_path, (source,))
    parent = output.parent
    if hardlink and source.stat().st_dev != parent.stat().st_dev:
        raise UserError("硬链接要求源和输出在同一卷。请选择同盘位置，或取消硬链接。")

    notify("正在读取内置校验数据…", 0, 0)
    catalogs = load_manifest(release_dir / "manifest.json.gz")
    expected = catalogs[target]
    peer = catalogs["bilibili" if target == "official" else "official"]
    files = snapshot(source)
    exe = source / GAME_EXE
    if not exe.is_file() or fingerprint(exe) != expected[GAME_EXE]:
        raise UserError(f"这个客户端的主程序与已验证版本 {RELEASE_VERSION} 不匹配。")

    if not matches(VERSION_MARKER, expected[INDEX_VERSION]):
        raise ValueError("The embedded version marker does not match the release.")
    sizes = {name: record[0] for name, record in expected.items()}
    total = sum(sizes.values())
    possible_shared = sum(
        sizes[name] for name in sizes
        if hardlink and name.startswith(RESOURCE_ROOT) and name.endswith(".r")
    )
    if shutil.disk_usage(parent).free < total - possible_shared:
        raise UserError("输出盘空间不足。请另选位置或释放磁盘空间。")

    notify("正在准备目标渠道的定位配置…", 0, 0)
    config = channel_config(source, target, expected[CHANNEL_CONFIG], release_dir)
    bootstrap = {CHANNEL_CONFIG: config, INDEX_VERSION: VERSION_MARKER}
    staged = Path(tempfile.mkdtemp(prefix=".azur-prepare-", dir=parent))
    linked_bytes = copied_bytes = prepared_count = 0
    progress_label = "正在逐文件校验并复用…" if verify_hashes else "正在复用已有文件…"
    try:
        for number, (name, record) in enumerate(expected.items(), start=1):
            notify(progress_label, number, len(expected))
            destination = staged / name
            if name in bootstrap:
                destination.parent.mkdir(parents=True, exist_ok=True)
                data = bootstrap[name]
                destination.write_bytes(data)
                copied_bytes += len(data)
            elif not name.startswith("_index/") and name in files and files[name][0] == record[0]:
                current = source / name
                if verify_hashes:
                    if fingerprint(current) != record:
                        continue
                elif peer.get(name) != record:
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                if hardlink and name.startswith(RESOURCE_ROOT) and name.endswith(".r"):
                    if getattr(current.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_READONLY:
                        raise UserError(f"资源为只读，无法安全创建硬链接。{name}")
                    os.link(current, destination)
                    linked_bytes += record[0]
                else:
                    shutil.copyfile(current, destination)
                    if verify_hashes and fingerprint(destination) != record:
                        raise ValueError(f"SHA-256 differs from target manifest: {current}")
                    copied_bytes += record[0]
            else:
                continue
            prepared_count += 1

        notify("正在检查源文件和启动器修复所需空间…", len(expected), len(expected))
        if snapshot(source) != files:
            raise UserError("准备期间源目录发生了变化。请关闭游戏和更新任务后重试。")
        missing_bytes = total - linked_bytes - copied_bytes
        if shutil.disk_usage(parent).free < missing_bytes:
            raise UserError(f"启动器修复还需要约 {missing_bytes / 10**9:.2f} GB 可用空间。")
        notify("正在生成目录…", len(expected), len(expected))
        staged.rename(output)
    finally:
        if staged.exists():
            if staged.resolve(strict=True) != staged or staged.parent != parent:
                raise ValueError(f"Unexpected temporary directory: {staged}")
            shutil.rmtree(staged)

    return Prepared(output, linked_bytes, copied_bytes, len(expected) - prepared_count, missing_bytes)
