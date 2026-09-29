#!/usr/bin/env bash
# piqnyx: runs the tests inside the old image and inside ours, and compares the two runs.
# SPDX-License-Identifier: AGPL-3.0
#
#     ./piqnyx/test-in-image.sh
#
# Touches no container that runs and no data. The tests run in containers of
# their own, as the user the server runs as, without network; the checkout is
# lent to them for reading only and what they write goes to memory and is gone
# with them. One step needs the network: taking pytest from PyPI into a folder
# of its own (PIQNYX.md, stage 2.5).
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

# Every run is taken anew: what an earlier one left is not to be trusted.
out="$WORK/tests"
mkdir -p "$WORK"
rm -rf "${out:?}"
mkdir -p "$out/tools"

common="$HERE/tests-inside.txt"
known=0
while IFS= read -r name || [ -n "$name" ]; do
    [ -n "$name" ] || continue
    git cat-file -e "$TAG:$name" 2> /dev/null ||
        stop "в списке общих тестов имя не из тега $TAG: $name"
    [ -e "$name" ] || stop "в каталоге нет теста из списка общих: $name"
    known=$((known + 1))
done < "$common"
[ "$known" -gt 0 ] || stop "список общих тестов пуст: $common"

# The tests of ours are those our branch added to the tests of upstream.
git diff --name-only --no-renames --diff-filter=A "$TAG" HEAD -- tests |
    { grep -E '(^|/)test_[^/]*\.py$' || true; } > "$out/ours.txt"
[ -s "$out/ours.txt" ] || stop "наша ветка не добавила ни одного теста: проверять в новом образе нечего"

echo "беру инструменты тестов с PyPI: $TEST_TOOLS"
echo "это единственный шаг, где контейнер ходит в сеть"
read -r -a tools <<< "$TEST_TOOLS"
docker run --rm --no-healthcheck --network host --user "$(id -u):$(id -g)" \
    -e HOME=/tmp -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$out/tools:/tools" \
    --entrypoint /usr/local/bin/python3 "$BASE" \
    -m pip install --quiet --no-cache-dir --no-compile --target /tools "${tools[@]}" ||
    stop "инструменты тестов не поставились"

put_away() {
    docker rm -f piqnyx-ov-tests-old > /dev/null 2>&1 || true
    docker rm -f piqnyx-ov-tests-new > /dev/null 2>&1 || true
}
trap put_away EXIT

inside() {
    local side="$1" image="$2" name="piqnyx-ov-tests-$1" code=0
    echo "гоняю тесты в образе $image, не дольше $TEST_LONGEST мин"
    docker rm -f "$name" > /dev/null 2>&1 || true
    timeout --signal=TERM --kill-after=30 "${TEST_LONGEST}m" \
        docker run --rm --name "$name" --no-healthcheck --network none --user "$RUN_AS" \
        --cpus "$TEST_CPUS" --memory "$TEST_MEMORY" \
        --tmpfs "/src/test_data:rw,exec,nosuid,nodev,size=4g,mode=0755,uid=${RUN_AS%%:*},gid=${RUN_AS##*:}" \
        -e HOME=/src/test_data/home \
        -e OPENVIKING_CONFIG_FILE=/src/ov.conf \
        -e PYTHONDONTWRITEBYTECODE=1 \
        -v "$ROOT/tests:/src/tests:ro" \
        -v "$ROOT/pyproject.toml:/src/pyproject.toml:ro" \
        -v "$HERE/test-ov.conf:/src/ov.conf:ro" \
        -v "$HERE/run_tests_inside.py:/src/run_tests_inside.py:ro" \
        -v "$common:/src/common.txt:ro" \
        -v "$out/ours.txt:/src/ours.txt:ro" \
        -v "$out/tools:/tools:ro" \
        --workdir /src --entrypoint python "$image" \
        /src/run_tests_inside.py --tools /tools --each "$TEST_EACH" \
        --side "$side" --common /src/common.txt --ours /src/ours.txt \
        > "$out/$side.out" 2> "$out/$side.rec" || code=$?
    docker rm -f "$name" > /dev/null 2>&1 || true
    echo "  кончено с кодом $code, записей $(grep -c '^@@piqnyx ' "$out/$side.rec" || true)"
}

inside old "$BASE"
inside new "$IMAGE"

echo
echo "== Сравнение прогонов: $BASE и $IMAGE"
failed=0
python3 "$HERE/compare_runs.py" \
    --old "$out/old.rec" --new "$out/new.rec" --ours "$out/ours.txt" || failed=1

echo
echo "что говорил pytest: $out/old.out и $out/new.out"
if [ "$failed" = "0" ]; then
    echo "ИТОГ ТЕСТОВ: образ $IMAGE ведёт себя в тестах как прежний, наши тесты в нём проходят"
else
    echo "ИТОГ ТЕСТОВ: ОСТАНОВКА, прогоны надо разобрать"
    exit 1
fi
