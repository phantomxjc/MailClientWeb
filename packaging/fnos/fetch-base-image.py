#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 python:3.12-slim 基础镜像下载成本地离线镜像包（docker save 格式）。

为什么需要它
------------
飞牛安装 Docker 类应用时会自己跑 `docker compose up`（镜像不存在就现场构建），
而国内绝大多数网络**连不上 Docker Hub**（registry-1.docker.io 直连超时），
于是构建第一步 `FROM python:3.12-slim` 就挂掉，安装报「无法安装」。

解决办法：在国内能访问的镜像加速站点上，用 Registry HTTP API 把基础镜像的
各层拉下来，就地组装成一份 `docker save` 格式的 tar.gz，随 FPK 一起发出去。
安装时 `docker load` 一下即可 —— 飞牛完全不需要访问任何镜像仓库。

产物：packaging/fnos/app/docker/base-image.tar.gz
      （内含 manifest.json + 镜像 config + 各层 layer.tar，RepoTags=python:3.12-slim）

用法：
    python packaging/fnos/fetch-base-image.py            # 需要时才下载
    python packaging/fnos/fetch-base-image.py --force    # 强制重下
    python packaging/fnos/fetch-base-image.py --verify   # 只校验已有产物
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import ssl
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent          # packaging/fnos
OUT = HERE / "app" / "docker" / "base-image.tar.gz"

# 目标镜像（必须与 Dockerfile 里 FROM 的默认值一致）
REPO = "library/python"
TAG = "3.12-slim"
PLATFORM = ("linux", "amd64")
SAVE_TAG = "python:3.12-slim"                   # docker load 后得到的标签

# 国内镜像加速站点。按顺序试，第一个成功即用。
# 这些站点都是 Docker Hub 的只读代理，路径与官方 Registry API 完全一致。
MIRRORS = [
    "docker.1panel.live",
    "docker.m.daocloud.io",
    "docker.1ms.run",
    "docker.xuanyuan.me",
    "hub.rat.dev",
]

ACCEPT_INDEX = ", ".join([
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
])
ACCEPT_MANIFEST = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_CTX = ssl.create_default_context()


def _request(url, accept=None, timeout=60, retries=2):
    """带重试的 GET，返回 (bytes)。网络抖动时自动重来。"""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url)
            if accept:
                req.add_header("Accept", accept)
            req.add_header("User-Agent", "docker/24.0.0")
            with _OPENER.open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:                    # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求失败 {url}：{last}")


def _pick_amd64(manifest_bytes: bytes, host: str):
    """清单可能是多平台的 index，挑出 linux/amd64 再取一次。"""
    doc = json.loads(manifest_bytes)
    if "manifests" not in doc:                    # 已经是单平台 manifest
        return doc
    for m in doc["manifests"]:
        p = m.get("platform") or {}
        if p.get("os") == PLATFORM[0] and p.get("architecture") == PLATFORM[1]:
            digest = m["digest"]
            raw = _request(f"https://{host}/v2/{REPO}/manifests/{digest}", ACCEPT_MANIFEST)
            return json.loads(raw)
    raise RuntimeError(f"{host} 的清单里没有 {PLATFORM[0]}/{PLATFORM[1]}")


def _decompress(blob: bytes, media_type: str) -> bytes:
    """把层 blob 解成未压缩的 tar 字节。registry 里一般是 gzip。"""
    if blob[:2] == b"\x1f\x8b" or "gzip" in media_type:
        return gzip.decompress(blob)
    if "zstd" in media_type:
        try:
            import zstandard                      # type: ignore
        except ImportError:
            raise RuntimeError("该层是 zstd 压缩，请先 pip install zstandard")
        return zstandard.ZstdDecompressor().decompress(blob)
    return blob


def fetch(force: bool = False) -> Path:
    if OUT.exists() and not force:
        print(f"→ 离线镜像包已存在，跳过下载：{OUT.name}"
              f"（{OUT.stat().st_size / 1024 / 1024:.1f} MB，--force 可强制重下）")
        return OUT

    OUT.parent.mkdir(parents=True, exist_ok=True)
    errors = []

    for host in MIRRORS:
        try:
            print(f"→ 尝试镜像源 {host} ...")
            idx = _request(f"https://{host}/v2/{REPO}/manifests/{TAG}", ACCEPT_INDEX, timeout=40)
            manifest = _pick_amd64(idx, host)

            cfg_digest = manifest["config"]["digest"]
            layers = manifest["layers"]
            cfg_raw = _request(f"https://{host}/v2/{REPO}/blobs/{cfg_digest}", timeout=60)
            cfg = json.loads(cfg_raw)
            diff_ids = cfg["rootfs"]["diff_ids"]

            if len(diff_ids) != len(layers):
                raise RuntimeError(
                    f"层数不一致：config 里 {len(diff_ids)} 层，manifest 里 {len(layers)} 层")

            total = sum(l.get("size", 0) for l in layers)
            print(f"  清单 OK：{len(layers)} 层，压缩体积约 {total / 1024 / 1024:.1f} MB")

            tmp = Path(tempfile.mkdtemp(prefix="mcbuild-"))
            try:
                # 1) 逐层下载 → 解压 → 算出 diffID（未压缩 tar 的 sha256）
                #    diffID 必须和 config.rootfs.diff_ids 完全对上，否则 docker load 会报
                #    「layer does not match this image」，所以这里当场比对。
                layer_files = []
                for i, l in enumerate(layers):
                    dgst = l["digest"]
                    size_mb = l.get("size", 0) / 1024 / 1024
                    print(f"  [{i + 1}/{len(layers)}] 下载 {dgst[:19]}... ({size_mb:.1f} MB)", end="", flush=True)
                    blob = _request(f"https://{host}/v2/{REPO}/blobs/{dgst}", timeout=600)
                    tar_bytes = _decompress(blob, l.get("mediaType", ""))
                    real = "sha256:" + hashlib.sha256(tar_bytes).hexdigest()

                    pf = tmp / f"layer{i}.tar"
                    pf.write_bytes(tar_bytes)
                    layer_files.append(pf)
                    ok = "✓" if real == diff_ids[i] else "✗"
                    print(f" 解出 {len(tar_bytes) / 1024 / 1024:.1f} MB  diffID {ok}")
                    if real != diff_ids[i]:
                        raise RuntimeError(
                            f"第 {i + 1} 层 diffID 不匹配（下载可能被截断）：\n"
                            f"     期望 {diff_ids[i]}\n     实际 {real}")
                    del blob, tar_bytes

                # 2) 组装 docker save 格式：manifest.json + config + <i>/layer.tar
                cfg_hex = cfg_digest.split(":", 1)[1]
                manifest_item = {
                    "Config": f"{cfg_hex}.json",
                    "RepoTags": [SAVE_TAG],
                    "Layers": [f"{i}/layer.tar" for i in range(len(layers))],
                }
                print(f"→ 组装 {OUT.name}（docker save 格式）...")
                with tarfile.open(OUT, "w:gz", compresslevel=6) as tf:
                    for name, data in ((f"{cfg_hex}.json", cfg_raw),
                                       ("manifest.json", json.dumps([manifest_item]).encode())):
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        info.mode = 0o644
                        info.mtime = int(time.time())
                        tf.addfile(info, io.BytesIO(data))
                    for i, pf in enumerate(layer_files):
                        info = tarfile.TarInfo(f"{i}/layer.tar")
                        info.size = pf.stat().st_size
                        info.mode = 0o644
                        info.mtime = int(time.time())
                        with open(pf, "rb") as fh:
                            tf.addfile(info, fh)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

            print(f"✓ 完成：{OUT}  （{OUT.stat().st_size / 1024 / 1024:.1f} MB）")
            return OUT

        except Exception as e:                    # noqa: BLE001
            print(f"  ✗ {host} 失败：{str(e)[:160]}")
            errors.append(f"{host}: {e}")
            if OUT.exists():
                OUT.unlink()

    raise SystemExit(
        "✗ 所有镜像源都失败了，没能取得基础镜像。\n   "
        + "\n   ".join(errors)
        + "\n   提示：可把可用站点加到本脚本的 MIRRORS 列表最前面再试。"
    )


def verify(path: Path = OUT) -> None:
    """校验产物的自洽性 —— 本机没有 docker，能查的先查干净。

    检查项：
      1. manifest.json / config / 每层 layer.tar 都在
      2. 每层 layer.tar 是合法 tar，且其 sha256 与 config.rootfs.diff_ids 一一对应
      3. RepoTags 里有 python:3.12-slim（否则 docker load 后没标签，构建仍会去拉网络）
      4. config 里的 architecture/os 是 linux/amd64
    """
    if not path.exists():
        raise SystemExit(f"✗ 找不到离线镜像包：{path}\n   先跑一次 fetch-base-image.py")

    problems = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with tarfile.open(path, "r:gz") as tf:
            names = tf.getnames()
            try:
                tf.extractall(tmp, filter="fully_trusted")   # Python 3.12+
            except TypeError:
                tf.extractall(tmp)

        if "manifest.json" not in names:
            raise SystemExit("✗ 包里没有 manifest.json")

        items = json.loads((tmp / "manifest.json").read_text())
        item = items[0]
        cfg = json.loads((tmp / item["Config"]).read_text())

        if SAVE_TAG not in item.get("RepoTags", []):
            problems.append(f"RepoTags 里没有 {SAVE_TAG}：{item.get('RepoTags')}")

        arch, os_ = cfg.get("architecture"), cfg.get("os")
        if (os_, arch) != PLATFORM:
            problems.append(f"平台不是 {PLATFORM[0]}/{PLATFORM[1]}，而是 {os_}/{arch}")

        diff_ids = cfg.get("rootfs", {}).get("diff_ids", [])
        if len(diff_ids) != len(item["Layers"]):
            problems.append(f"层数不匹配：config {len(diff_ids)} vs manifest {len(item['Layers'])}")

        for i, rel in enumerate(item["Layers"]):
            p = tmp / rel
            if not p.exists():
                problems.append(f"缺少层文件：{rel}")
                continue
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                while chunk := fh.read(1024 * 1024):
                    h.update(chunk)
            got = "sha256:" + h.hexdigest()
            if i < len(diff_ids) and got != diff_ids[i]:
                problems.append(f"{rel} 的 diffID 与 config 不符")
            try:                                   # 确认是合法 tar（不是被截断的）
                with tarfile.open(p, "r:") as lt:
                    lt.next()
            except Exception as e:                 # noqa: BLE001
                problems.append(f"{rel} 不是合法 tar：{e}")

    if problems:
        print("✗ 离线镜像包自检未通过：")
        for x in problems:
            print("   -", x)
        sys.exit(1)

    print(f"✓ 离线镜像包自检通过：{len(item['Layers'])} 层、diffID 全部对应、"
          f"标签 {SAVE_TAG}、平台 {PLATFORM[1]}"
          f"（{path.stat().st_size / 1024 / 1024:.1f} MB）")


def main() -> None:
    ap = argparse.ArgumentParser(description="下载 python:3.12-slim 并打成离线镜像包")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    ap.add_argument("--verify", action="store_true", help="只校验已有产物，不下载")
    args = ap.parse_args()

    if args.verify:
        verify()
        return

    if os.environ.get("SKIP_BASE_IMAGE"):
        print("→ 已设置 SKIP_BASE_IMAGE，跳过离线镜像包（构建出来的 FPK 需要 NAS 联网拉镜像）")
        return

    fetch(force=args.force)
    verify()


if __name__ == "__main__":
    main()
