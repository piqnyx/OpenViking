#!/usr/bin/env bash
# piqnyx: checks that our image is the running one with our files laid over it.
# SPDX-License-Identifier: AGPL-3.0
#
#     ./piqnyx/verify-image.sh
#
# Touches no container that runs and no data: the images are looked at in
# containers of their own, without network and without mounts, that are gone
# when the look is over (PIQNYX.md, stage 2).
set -euo pipefail
# shellcheck source=piqnyx/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$ROOT"

docker image inspect "$BASE" > /dev/null 2>&1 ||
    stop "исходного образа нет на диске: $BASE"
docker image inspect "$IMAGE" > /dev/null 2>&1 ||
    stop "нашего образа нет на диске: $IMAGE. Сперва сборка: ./piqnyx/build.sh"
git rev-parse --quiet --verify "refs/tags/$TAG^{commit}" > /dev/null ||
    stop "в каталоге нет тега $TAG: git fetch --tags"

# Every check is taken anew: what an earlier one left is not to be trusted.
check="$WORK/check"
mkdir -p "$WORK"
rm -rf "${check:?}"
mkdir -p "$check/tag"

look() {
    docker run --rm --no-healthcheck --network none "$@"
}

listing() {
    echo "читаю образ $1"
    look --user 0:0 --entrypoint /bin/sh "$1" -c \
        'find / -xdev -type f -exec sha256sum {} +' > "$check/$2.sha" ||
        stop "список файлов образа $1 не получен"
    look --user 0:0 --entrypoint /bin/sh "$1" -c \
        "find / -xdev -printf '%y\t%m\t%U:%G\t%p\t%l\n'" > "$check/$2.ent" ||
        stop "список файлов образа $1 с правами не получен"
    docker image inspect --format '{{json .Config}}' "$1" > "$check/$2.json" ||
        stop "настройки образа $1 не получены"
}

listing "$BASE" old
listing "$IMAGE" new

site="$(look --user "$RUN_AS" --entrypoint python "$IMAGE" -B -c \
    'import sysconfig; print(sysconfig.get_path("purelib"))')" ||
    stop "Python в образе $IMAGE не ответил, где лежат пакеты"
[ "$site" = "$SITE" ] ||
    stop "пакеты в образе лежат в $site, а в piqnyx/image.conf записано $SITE"
cache_tag="$(look --user "$RUN_AS" --entrypoint python "$IMAGE" -B -c \
    'import sys; print(sys.implementation.cache_tag)')" ||
    stop "Python в образе $IMAGE не ответил, как зовёт свой байткод"

git archive "$TAG" openviking openviking_cli | tar -x -C "$check/tag" ||
    stop "файлы тега $TAG не выгружены"
printf '%s\n' "$VERSION" > "$check/version.txt"

failed=0

echo
echo "== Сверка образов: $BASE и $IMAGE"
python3 "$HERE/compare_images.py" \
    --old-files "$check/old.sha" --new-files "$check/new.sha" \
    --old-entries "$check/old.ent" --new-entries "$check/new.ent" \
    --old-config "$check/old.json" --new-config "$check/new.json" \
    --overlay "$HERE/overlay.txt" --version "$check/version.txt" \
    --site "$SITE" --cache-tag "$cache_tag" \
    --source-root "$ROOT" --tag-root "$check/tag" || failed=1

echo
echo "== Проверка изнутри образа, от пользователя $RUN_AS"
look -i --user "$RUN_AS" --entrypoint python "$IMAGE" - "$SITE" \
    --first openviking_cli.server_bootstrap \
    --first openviking.server.bootstrap \
    --package openviking --package-version "$PACKAGE_VERSION" \
    < "$HERE/inside_check.py" || failed=1

echo
if [ "$failed" = "0" ]; then
    echo "ИТОГ ПРОВЕРКИ: образ $IMAGE годен, работающий контейнер не тронут"
else
    echo "ИТОГ ПРОВЕРКИ: ОСТАНОВКА, образ $IMAGE ставить нельзя"
    exit 1
fi
