#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 MailClient Web 打包成飞牛 fnOS 可安装的 .fpk 文件。

它做四件事：
  0. 准备离线基础镜像 python:3.12-slim（见 fetch-base-image.py）——
     国内连不上 Docker Hub，没有它飞牛安装时构建必挂
  1. 把项目源码同步进 app/docker/src/ —— 源码随包走，镜像不存在时可就地构建
     （严格白名单，data/ 里的凭据、邮件绝不会进包）
  2. 调用飞牛官方 fnpack 生成 .fpk，产物落到项目根的 dist/
  3. 解开产物自检：结构 / JSON / 行尾 / 敏感文件 / 离线镜像的层 diffID

用法：
    python packaging/fnos/build-fpk.py
    FNPACK_BIN=/usr/local/bin/fnpack python packaging/fnos/build-fpk.py
    SKIP_BASE_IMAGE=1 python packaging/fnos/build-fpk.py   # 不打离线镜像（不推荐）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent        # packaging/fnos
PROJECT = HERE.parent.parent                  # 项目根
SRC = HERE / "app" / "docker" / "src"
DIST = PROJECT / "dist"

# 进包的源码白名单 —— 显式列出来，避免哪天手滑把 data/ 打进去
FILES = [
    "app.py", "auth.py", "accounts.py", "config.py", "db.py",
    "mail_conn.py", "msauth.py", "notifier.py", "oauth.py", "parser.py",
    "sender.py", "sync.py", "requirements.txt", "Dockerfile", ".dockerignore",
]
DIRS = ["templates", "static"]

IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", "*.db", "*.db-wal", "*.db-shm",
    "*.key", "*.log", ".DS_Store", ".env", "_shot*",
)


def find_fnpack() -> str:
    """按 环境变量 → 包内自带 → PATH 的顺序找 fnpack。"""
    env = os.environ.get("FNPACK_BIN", "").strip()
    if env:
        if not Path(env).exists():
            sys.exit(f"✗ FNPACK_BIN 指向的文件不存在：{env}")
        return env
    for name in ("fnpack.exe", "fnpack"):
        local = HERE / name
        if local.exists():
            return str(local)
    found = shutil.which("fnpack")
    if found:
        return found
    sys.exit(
        "✗ 找不到 fnpack。三种解决办法：\n"
        "   1) 下载后放到 packaging/fnos/fnpack（Windows 下为 fnpack.exe）\n"
        "      下载地址：https://developer.fnnas.com/docs/cli/fnpack/\n"
        "   2) 设置环境变量 FNPACK_BIN=/绝对路径/fnpack\n"
        "   3) 把 fnpack 装进 PATH"
    )


def ensure_base_image() -> None:
    """准备随包的离线基础镜像。

    飞牛安装时会自己 compose up（现场构建），而国内连不上 Docker Hub，
    所以把 python:3.12-slim 变成一份 docker save 出来的 tar.gz 一起发出去。
    设 SKIP_BASE_IMAGE=1 可跳过（不打离线镜像，NAS 侧需能访问镜像站）。
    """
    if os.environ.get("SKIP_BASE_IMAGE"):
        print("→ SKIP_BASE_IMAGE 已设置：不打离线镜像包")
        return
    script = HERE / "fetch-base-image.py"
    if not script.exists():
        sys.exit(f"✗ 找不到 {script}，无法准备离线基础镜像")
    print("→ 准备离线基础镜像包（已有则跳过）...")
    proc = subprocess.run([sys.executable, str(script)], cwd=str(HERE))
    if proc.returncode != 0:
        sys.exit("✗ 离线基础镜像准备失败，打出来的包在离线环境装不上。\n"
                 "   也可以设 SKIP_BASE_IMAGE=1 强行跳过（不推荐）")


def _reset_dir(path) -> None:
    """清空并重建目录。

    为什么不直接 shutil.rmtree 了事：WorkBuddy 沙箱里它会被安全垫片接管成
    「移到回收站」，而且有两种表现 —— 有时静默把目录移走并正常返回，有时抛
    SHFileOperationW 0x2。前一种最阴：rmtree 返回了，目录却已经不在了，
    下一个 copy 直接 FileNotFoundError（第二次打包必崩）。

    所以以「改名让开」为主：把旧目录挪到包外的 _stale 再重建。无论 _stale 里的
    东西最终有没有被真删掉都不影响打包 —— _stale 在 app/ 外面，fnpack 不会
    把它打进包。
    """
    if path.exists():
        stale = HERE / "_stale"
        moved = False
        try:
            if stale.exists():
                shutil.rmtree(stale, ignore_errors=True)
            if stale.exists():              # 删不掉就让一步，别挡路
                stale.rename(HERE / f"_stale_{int(time.time())}")
            path.rename(stale)
            moved = True
        except Exception:                   # noqa: BLE001
            pass
        if not moved:
            shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        sys.exit(f"✗ 无法准备目录 {path}\n"
                 "   可能被沙箱 / 杀软 / 编辑器占用，手动删掉它再试一次")


def sync_source() -> int:
    """把源码同步进 app/docker/src/，返回复制的文件数。"""
    _reset_dir(SRC)

    count = 0
    missing = []
    for name in FILES:
        s = PROJECT / name
        if s.is_file():
            shutil.copy2(s, SRC / name)
            count += 1
        else:
            missing.append(name)
    for name in DIRS:
        s = PROJECT / name
        if s.is_dir():
            shutil.copytree(s, SRC / name, ignore=IGNORE, dirs_exist_ok=True)
            count += sum(1 for _ in (SRC / name).rglob("*") if _.is_file())
        else:
            missing.append(name + "/")

    if missing:
        print(f"  ! 以下条目在项目里没找到，已跳过：{', '.join(missing)}")

    # 提醒：根目录若出现没被纳入的 .py，多半是新增模块忘了加进白名单
    known = set(FILES)
    extra = sorted(
        p.name for p in PROJECT.glob("*.py")
        if p.name not in known and not p.name.startswith("_")
    )
    if extra:
        print(f"  ! 项目根目录有未纳入白名单的 Python 文件：{', '.join(extra)}")
        print("    （确实是应用代码的话，请加进本脚本的 FILES 列表）")

    return count


def normalize_eol() -> int:
    """把包内所有文本文件统一成 LF。

    Windows 上编辑过的文件会带上 CRLF，而飞牛是 Linux ——
    cmd/ 下的脚本一旦是 CRLF，执行时会直接报 `bad interpreter: /bin/bash^M`。
    所以每次打包前都强制归一，别指望手动改。
    """
    binary = {".exe", ".png", ".jpg", ".ico", ".gz", ".tgz", ".fpk", ".zip"}
    changed = 0
    for p in HERE.rglob("*"):
        if not p.is_file() or p.suffix.lower() in binary:
            continue
        if p.name.startswith(".") and p.name not in {".dockerignore"}:
            continue
        raw = p.read_bytes()
        fixed = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if fixed != raw:
            p.write_bytes(fixed)
            changed += 1
    return changed


def fix_packaged_text(fpk) -> int:
    """把包内文本成员的行尾修正回 LF，返回修正的文件数。

    坑：Windows 版 fnpack 会把 manifest 解析后重新序列化，行尾变成 CRLF
    （源文件明明是 LF，但打出来的包里每行末尾都多了 \\r）。
    飞牛是 Linux，带 \\r 的元数据可能被解析成值的一部分。
    这里重建一次 tar.gz：除内容外，权限位、时间戳、成员顺序全部原样保留。
    """
    import io
    import tarfile

    binary = (".png", ".tgz", ".jpg", ".ico", ".gz", ".exe", ".fpk", ".zip")
    tmp = fpk.with_name(fpk.name + ".tmp")
    changed = 0

    with tarfile.open(fpk, "r:gz") as src, tarfile.open(tmp, "w:gz") as dst:
        for m in src.getmembers():
            data = None
            if m.isfile():
                handle = src.extractfile(m)
                data = handle.read() if handle else b""
                if b"\r" in data and not m.name.lower().endswith(binary):
                    fixed = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    if fixed != data:
                        data = fixed
                        m.size = len(data)
                        changed += 1
            dst.addfile(m, io.BytesIO(data) if data is not None else None)

    tmp.replace(fpk)
    return changed


def _extract(tf, dest):
    """兼容不同 Python 版本的 tarfile 提取（3.12+ 要求显式 filter）。"""
    try:
        tf.extractall(dest, filter="fully_trusted")
    except TypeError:
        tf.extractall(dest)


def check_base_image(path):
    """校验随包的离线基础镜像 —— 它决定「离线安装」能不能成，必须验。

    按 docker save 格式逐项核对：manifest.json 在不在、标签对不对、
    每一层的 sha256（diffID）跟 config 里的 rootfs.diff_ids 是否一一对应。
    diffID 对不上，docker load 到飞牛上会直接失败。
    全程流式读取，不解压到磁盘。
    """
    import hashlib
    import json
    import tarfile

    probs = []
    try:
        with tarfile.open(path, "r:gz") as tf:
            names = tf.getnames()
            if "manifest.json" not in names:
                return [f"{path.name} 里没有 manifest.json（不是 docker save 格式）"]

            item = json.loads(tf.extractfile("manifest.json").read())[0]
            cfg = json.loads(tf.extractfile(item["Config"]).read())

            if "python:3.12-slim" not in item.get("RepoTags", []):
                probs.append(f"离线镜像标签不对：{item.get('RepoTags')}")

            d_ids = cfg.get("rootfs", {}).get("diff_ids", [])
            if len(d_ids) != len(item["Layers"]):
                probs.append(f"离线镜像层数不符：config {len(d_ids)} vs manifest {len(item['Layers'])}")

            for i, rel in enumerate(item["Layers"]):
                fh = tf.extractfile(rel)
                if fh is None:
                    probs.append(f"离线镜像缺少层文件 {rel}")
                    continue
                h = hashlib.sha256()
                while chunk := fh.read(4 << 20):
                    h.update(chunk)
                if i < len(d_ids) and "sha256:" + h.hexdigest() != d_ids[i]:
                    probs.append(f"离线镜像 {rel} 的 diffID 与 config 不符（镜像包损坏）")
    except Exception as e:                          # noqa: BLE001
        probs.append(f"离线镜像包无法解析：{e}")
    return probs


def verify(fpk) -> None:
    """解开刚打好的包做一轮自检 —— 本机没有飞牛环境，能验的先全验掉。"""
    import json
    import tarfile
    import tempfile

    problems = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with tarfile.open(fpk, "r:gz") as tf:
            _extract(tf, tmp)

        # 1) 飞牛硬性要求的路径
        for rel in ("manifest", "config/privilege", "config/resource",
                    "ICON.PNG", "ICON_256.PNG", "app.tgz", "cmd", "wizard"):
            if not (tmp / rel).exists():
                problems.append(f"缺少必需项：{rel}")

        # 2) manifest 关键字段
        mf = (tmp / "manifest").read_text(encoding="utf-8")
        for key in ("appname", "version", "display_name", "platform",
                    "service_port", "desktop_uidir", "desktop_applaunchname"):
            if key not in mf:
                problems.append(f"manifest 缺少字段：{key}")

        # 3) 展开 app.tgz
        app = tmp / "app"
        app.mkdir(exist_ok=True)
        with tarfile.open(tmp / "app.tgz", "r:gz") as tf:
            _extract(tf, app)

        # 4) 应用文件是否齐
        for rel in ("docker/docker-compose.yaml", "docker/src/Dockerfile",
                    "docker/src/requirements.txt", "docker/src/app.py",
                    "ui/config", "ui/images/icon_64.png", "ui/images/icon_256.png"):
            if not (app / rel).exists():
                problems.append(f"app.tgz 内缺少：{rel}")

        # 4b) 离线基础镜像 —— 离线安装全靠它，逐项验到层 diffID
        helper = app / "docker/ensure-base-image.sh"
        if not helper.exists():
            problems.append("app.tgz 内缺少：docker/ensure-base-image.sh")
        base = app / "docker/base-image.tar.gz"
        if os.environ.get("SKIP_BASE_IMAGE"):
            print("  ! 跳过了离线镜像检查（SKIP_BASE_IMAGE=1）")
        elif not base.exists():
            problems.append("app.tgz 内缺少离线基础镜像 docker/base-image.tar.gz"
                            "（否则 NAS 必须能连镜像站才能装，可用 SKIP_BASE_IMAGE=1 跳过检查）")
        else:
            probs = check_base_image(base)
            problems.extend(probs)
            if not probs:
                print(f"  离线基础镜像 OK：{base.stat().st_size / 1024 / 1024:.1f} MB，"
                      "各层 diffID 与 config 一致")

        # 5) JSON 是否合法
        for rel in (tmp / "config/privilege", tmp / "config/resource",
                    app / "ui/config", tmp / "wizard/install"):
            try:
                json.loads(rel.read_text(encoding="utf-8"))
            except Exception as e:
                problems.append(f"{rel.name} 不是合法 JSON：{e}")

        # 6) 脚本行尾必须是 LF —— CRLF 会让飞牛报 bad interpreter: /bin/bash^M
        for p in list((tmp / "cmd").rglob("*")) + [tmp / "manifest",
                                                   tmp / "wizard/install"]:
            if p.is_file() and b"\r" in p.read_bytes():
                problems.append(f"{p.relative_to(tmp)} 含 CR 字符（必须是 LF）")

        # 6b) 换源链路要接得上：compose 有 PY_BASE、钩子真的引用了辅助脚本
        compose_txt = (app / "docker/docker-compose.yaml").read_text(encoding="utf-8")
        if "PY_BASE" not in compose_txt:
            problems.append("docker-compose.yaml 没有 PY_BASE 构建参数，基础镜像无法换源")
        cb = (tmp / "cmd/install_callback").read_text(encoding="utf-8")
        if "ensure-base-image.sh" not in cb:
            problems.append("install_callback 没有引用 ensure-base-image.sh，离线镜像不会被导入")
        # 辅助脚本是被 source 执行的，带 CRLF 会直接报错，单独再扫一遍
        for p in (app / "docker/ensure-base-image.sh", app / "docker/docker-compose.yaml"):
            if p.is_file() and b"\r" in p.read_bytes():
                problems.append(f"{p.relative_to(tmp)} 含 CR 字符（必须是 LF）")

        # 7) 凭据 / 数据库绝不能进包
        for p in tmp.rglob("*"):
            if p.is_file() and (p.suffix in (".db", ".key", ".db-wal", ".db-shm")
                                or p.name in (".auth", ".env")):
                problems.append(f"敏感文件被打进包了：{p.relative_to(tmp)}")

    if problems:
        print("\n✗ 自检未通过：")
        for x in problems:
            print("   -", x)
        sys.exit(1)

    size = fpk.stat().st_size / 1024
    print(f"✓ 自检通过：结构完整、JSON 合法、脚本为 LF、无敏感文件（{size:.0f} KB）")


def main() -> None:
    fnpack = find_fnpack()
    print(f"→ fnpack：{fnpack}")

    ensure_base_image()

    n_fix = normalize_eol()
    print(f"→ 行尾符归一：{'转换了 ' + str(n_fix) + ' 个文件' if n_fix else '全部已是 LF'}")

    print("→ 同步源码到 app/docker/src/ ...")
    n = sync_source()
    print(f"  已复制 {n} 个文件")

    # 兜底：确认没有密钥/数据库混进去
    leaked = [p for p in SRC.rglob("*")
              if p.is_file() and (p.suffix in (".db", ".key") or p.name == ".auth")]
    if leaked:
        sys.exit("✗ 检测到敏感文件被复制进包，已中止：\n   "
                 + "\n   ".join(str(p) for p in leaked))

    print("→ 调用 fnpack 打包 ...")
    # fnpack 的产物固定落在「进程工作目录」，所以 cwd 指到应用目录
    proc = subprocess.run([fnpack, "build"], cwd=str(HERE))
    if proc.returncode != 0:
        sys.exit(f"✗ fnpack 打包失败（退出码 {proc.returncode}）")

    fpk = list(HERE.glob("*.fpk")) + list(PROJECT.glob("mailclientweb.fpk"))
    if not fpk:
        sys.exit("✗ 打包命令成功，但没找到 .fpk 产物")

    DIST.mkdir(exist_ok=True)
    for f in fpk:
        target = DIST / f.name
        shutil.move(str(f), str(target))
        print(f"\n→ 产物：{target}")
        n_txt = fix_packaged_text(target)
        if n_txt:
            print(f"→ 已修正包内文本行尾：{n_txt} 个文件"
                  "（Windows 版 fnpack 会把 manifest 写成 CRLF）")
        verify(target)


if __name__ == "__main__":
    main()
