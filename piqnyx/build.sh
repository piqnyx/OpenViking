#!/usr/bin/env bash
# piqnyx: builds our image: the running one with our files laid over it.
# SPDX-License-Identifier: AGPL-3.0
#
#     ./piqnyx/build.sh
#
# Touches no container. Builds from what is committed, and only when the list
# of our files is the difference of our branch from the tag (PIQNYX.md, stage 2).
# What comes out is to be checked by ./piqnyx/verify-image.sh before any use.
set -euo pipefail
# shellcheck source=piqnyx/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$ROOT"

left="$(git status --porcelain --untracked-files=all -- openviking openviking_cli piqnyx)"
[ -z "$left" ] || stop "в каталоге есть незакоммиченное, образ был бы собран не из коммита:
$left"

git rev-parse --quiet --verify "refs/tags/$TAG^{commit}" > /dev/null ||
    stop "в каталоге нет тега $TAG: git fetch --tags"

mkdir -p "$WORK"
git diff --name-status --no-renames "$TAG" HEAD > "$WORK/changes.txt"
python3 "$HERE/check_list.py" --changes "$WORK/changes.txt" --overlay "$HERE/overlay.txt" ||
    stop "список piqnyx/overlay.txt не равен разнице ветки с тегом $TAG"

docker buildx version > /dev/null 2>&1 ||
    stop "у docker нет BuildKit (docker buildx): рецепт даёт шагу сборки исходники взаймы, старый сборщик так не умеет"

docker image inspect "$BASE" > /dev/null 2>&1 ||
    stop "исходного образа нет на диске. Вернуть его: docker pull $BASE"

used="$(docker ps -a --filter "ancestor=$IMAGE" --format '{{.Names}}')"
[ -z "$used" ] || stop "образ $IMAGE уже в работе у контейнера: $used
Новая сборка берёт новую версию: строка VERSION в piqnyx/image.conf"

commit="$(git rev-parse HEAD)"
echo "собираю $IMAGE из коммита $commit"
DOCKER_BUILDKIT=1 docker build \
    --progress plain \
    --network none \
    --file "$HERE/Dockerfile" \
    --build-arg "BASE=$BASE" \
    --build-arg "SITE=$SITE" \
    --build-arg "VERSION=$VERSION" \
    --label "org.piqnyx.version=$VERSION" \
    --label "org.piqnyx.commit=$commit" \
    --label "org.piqnyx.base=$BASE" \
    --tag "$IMAGE" \
    "$ROOT" || stop "сборка не прошла, образа $IMAGE нет"

echo "СОБРАНО: $IMAGE"
echo "Работающий контейнер не тронут. Дальше проверка: ./piqnyx/verify-image.sh"
