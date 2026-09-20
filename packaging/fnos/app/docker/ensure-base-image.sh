#!/bin/bash
# 星尘邮箱 邮箱 —— 基础镜像保障（被 install_callback / upgrade_callback 复用）
#
# 背景：飞牛安装 Docker 应用时会自己执行 `docker compose up`（镜像不存在就现场构建），
# 而国内大网络环境普遍连不上 Docker Hub，构建第一步 `FROM python:3.12-slim`
# 就会超时，安装直接报「无法安装」。
#
# 这个脚本用三步把基础镜像准备好，让构建**一次网络请求都不用发**：
#   1) 优先用随包携带的离线镜像（docker save 出来的 tar.gz）
#   2) 导入后真的跑一次 `python -V` 验证，损坏就删掉重来
#   3) 兜底才去国内镜像站逐个拉取
#
# 用法（在 install_callback / upgrade_callback 里）：
#   . "${TRIM_APPDEST}/docker/ensure-base-image.sh"
#   ensure_base_image "${TRIM_APPDEST}" "$LOG_FILE" "${PY_BASE}"

BASE_IMAGE="python:3.12-slim"

# 国内镜像加速站（Docker Hub 只读代理）。按顺序试，成功即止。
BASE_IMAGE_MIRRORS="
docker.1panel.live
docker.m.daocloud.io
docker.1ms.run
docker.xuanyuan.me
hub.rat.dev
"

# 离线镜像导进来了没有？——同时验证真的能跑，避免「能 inspect 但一跑就炸」
_offline_image_usable () {
    docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || return 1
    if docker run --rm --network none --entrypoint python "$BASE_IMAGE" -V >> "$LOG_FILE" 2>&1; then
        return 0
    fi
    log "导入的镜像跑不起来（可能损坏或架构不符），删除后改用网络镜像源"
    docker rmi -f "$BASE_IMAGE" >> "$LOG_FILE" 2>&1
    return 1
}

ensure_base_image () {
    APPDEST="$1"
    LOG_FILE="$2"
    PY_BASE="${3:-python:3.12-slim}"
    TARBALL="${APPDEST}/docker/base-image.tar.gz"

    if ! command -v docker >/dev/null 2>&1; then
        log "docker 命令不可用，跳过基础镜像准备"
        return 0
    fi

    if _offline_image_usable; then
        log "基础镜像已就绪：$BASE_IMAGE"
    else
        # ---- 第一步：随包的离线镜像 ----
        if [ -f "$TARBALL" ]; then
            log "开始导入随包离线镜像 base-image.tar.gz（$(wc -c < "$TARBALL" | awk '{printf "%.0f MB", $1/1048576}')）"
            if docker load -i "$TARBALL" >> "$LOG_FILE" 2>&1; then
                log "离线镜像导入完成"
            else
                log "离线镜像导入失败，转用网络镜像源"
            fi
        else
            log "包里没有离线镜像文件（$TARBALL），直接走网络镜像源"
        fi

        # ---- 第二步：真跑一次验证 / 网络兜底 ----
        if ! _offline_image_usable; then
            for host in $BASE_IMAGE_MIRRORS; do
                log "尝试从国内镜像站拉取：$host"
                if docker pull "${host}/library/python:3.12-slim" >> "$LOG_FILE" 2>&1; then
                    docker tag "${host}/library/python:3.12-slim" "$BASE_IMAGE" >> "$LOG_FILE" 2>&1
                    log "已从 $host 取得基础镜像"
                    break
                fi
                log "  $host 不可用"
            done
        fi
    fi

    # ---- 第三步：让 compose 里配置的那个名字也能命中本地镜像 ----
    # compose 的 PY_BASE 默认写成国内镜像站的完整名（docker.1panel.live/library/python:3.12-slim），
    # 本地打完标签后构建器就直接用本地镜像，不会再访问任何仓库。
    if docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 && [ "$PY_BASE" != "$BASE_IMAGE" ]; then
        docker tag "$BASE_IMAGE" "$PY_BASE" >> "$LOG_FILE" 2>&1 \
            && log "已把本地镜像标记为 $PY_BASE（构建时不再联网）"
    fi

    if docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
        return 0
    fi
    log "警告：基础镜像仍未就绪，构建需要能连上镜像站才能成功"
    return 1
}
