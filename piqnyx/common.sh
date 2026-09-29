# piqnyx: what the scripts of the image share.
# SPDX-License-Identifier: AGPL-3.0
# shellcheck shell=bash
#
# Read by the scripts of this folder, not run by itself.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$HERE/.work"

stop() {
    echo "ОСТАНОВКА: $*" >&2
    exit 1
}

# A fact of piqnyx/image.conf: NAME=value, one a line, given once.
fact() {
    local found
    found="$(grep -c "^$1=" "$HERE/image.conf" || true)"
    [ "$found" = "1" ] || stop "в piqnyx/image.conf факт $1 дан $found раз, а нужен один"
    found="$(sed -n "s/^$1=//p" "$HERE/image.conf")"
    [ -n "$found" ] || stop "в piqnyx/image.conf факт $1 пуст"
    printf '%s' "$found"
}

[ -f "$HERE/image.conf" ] || stop "нет файла фактов: $HERE/image.conf"
TAG="$(fact TAG)"
PACKAGE_VERSION="$(fact PACKAGE_VERSION)"
BASE="$(fact BASE)"
SITE="$(fact SITE)"
RUN_AS="$(fact RUN_AS)"
NAME="$(fact NAME)"
VERSION="$(fact VERSION)"
IMAGE="$NAME:$VERSION"
TEST_TOOLS="$(fact TEST_TOOLS)"
TEST_EACH="$(fact TEST_EACH)"
TEST_LONGEST="$(fact TEST_LONGEST)"
TEST_CPUS="$(fact TEST_CPUS)"
TEST_MEMORY="$(fact TEST_MEMORY)"
COMPOSE="$(fact COMPOSE)"
SERVICE="$(fact SERVICE)"
CONTAINER="$(fact CONTAINER)"
HEALTHY_WITHIN="$(fact HEALTHY_WITHIN)"

for tool in docker git python3; do
    command -v "$tool" > /dev/null 2>&1 || stop "нет программы $tool"
done
