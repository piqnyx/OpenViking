#!/usr/bin/env bash
# piqnyx: puts our image in the place of the running one, and gives it back if it does not rise.
# SPDX-License-Identifier: AGPL-3.0
#
#     ./piqnyx/replace-container.sh          shows what would change, touches nothing
#     ./piqnyx/replace-container.sh --do     replaces the container
#     ./piqnyx/replace-container.sh --back   brings the old one back
#
# In the compose file the line of the image changes and `pull_policy: never` is
# put after it; nothing else. Before anything is touched the image is checked
# once more, the archives of the sessions are looked at (the old server marks an
# archive in work as failed when it is stopped), a copy of the compose file and
# what the old container said are kept. A new server that does not get well in
# time is taken away and the old one is brought back (PIQNYX.md, stage 2.6).
#
# The file may name the image we built on or an earlier image of ours (a second
# build takes the place of the first, PLAN-gorizont 3д). The copy kept is always
# the file before the latest replacement; an older copy is put aside, numbered
# (`docker-compose.yml.before-piqnyx.1`, `.2`, ...). `--back` leads to the file
# before the latest replacement.
set -euo pipefail
# shellcheck source=piqnyx/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$ROOT"

mode="show"
case "${1:-}" in
    "") ;;
    --do) mode="do" ;;
    --back) mode="back" ;;
    *) stop "не знаю ключа $1. Без ключа -- показать, --do -- заменить, --back -- вернуть прежний" ;;
esac

compose="$(realpath -m "$COMPOSE")"
home="$(dirname "$compose")"
kept="$compose.before-piqnyx"
work="$WORK/replace"
new="$work/docker-compose.new.yml"
look_every="${PIQNYX_LOOK_EVERY:-2}"

[ -f "$compose" ] || stop "нет compose-файла: $compose"
docker compose version > /dev/null 2>&1 || stop "у docker нет compose"
mkdir -p "$work"

in_compose() {
    docker compose --project-directory "$home" -f "$1" "${@:2}"
}

# What the container is: the image it runs on, its state, its health. Nothing, when there is none:
# what docker prints while it refuses is not taken for an answer.
container() {
    local told
    told="$(docker inspect --format \
        '{{.Image}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "$CONTAINER" 2> /dev/null)" || return 0
    printf '%s' "$told"
}

id_of() {
    local told
    told="$(docker image inspect --format '{{.Id}}' "$1" 2> /dev/null)" || return 0
    printf '%s' "$told"
}

archives() {
    local folder
    folder="$(python3 "$HERE/edit_compose.py" --file "$1" --service "$SERVICE" --data-folder)" || {
        echo "$folder"
        return 1
    }
    python3 "$HERE/pending_archives.py" "$(cd "$home" && realpath -m "$folder")"
}

said_by() {
    (umask 077 && docker logs "$CONTAINER" > "$work/$1" 2>&1) || true
}

# Waits till the container is well. Says why, when it is not to be waited for.
well_in_time() {
    local waited=0 state image status health
    while [ "$waited" -lt "$HEALTHY_WITHIN" ]; do
        sleep "$look_every"
        waited=$((waited + look_every))
        state="$(container)"
        read -r image status health <<< "${state:-none none none}"
        if [ "$status" = "running" ] && [ "$health" = "healthy" ]; then
            echo "  здоров через $waited с"
            return 0
        fi
        case "$status $health" in
            "running starting" | "running none" | "created "* | "restarting "*) ;;
            *)
                echo "  упал: состояние $status, здоровье $health"
                return 1
                ;;
        esac
        if [ $((waited % 10)) -lt "$look_every" ]; then
            echo "  ждём: $waited с, состояние $status, здоровье $health"
        fi
    done
    echo "  не стал здоровым за $HEALTHY_WITHIN с"
    return 1
}

put() {
    # Written into the file that is there: it keeps its mode and its owner.
    cat "$1" > "$compose"
}

bring_up() {
    in_compose "$compose" down || true
    in_compose "$compose" up -d
}

# ---------------------------------------------------------------- the way back

if [ "$mode" = "back" ]; then
    [ -f "$kept" ] || stop "нет копии прежнего compose-файла: $kept"
    was="$(python3 "$HERE/edit_compose.py" --file "$kept" --service "$SERVICE" --image-now)" ||
        stop "копию не прочитать: $kept"
    was_id="$(id_of "$was")"
    [ -n "$was_id" ] || stop "прежнего образа нет на диске: $was"
    read -r image status health <<< "$(container)"
    if cmp -s "$kept" "$compose" && [ "${image:-}" = "$was_id" ] && [ "$health" = "healthy" ]; then
        echo "ИТОГ ВОЗВРАТА: возвращать нечего, прежний сервер уже работает на $was"
        exit 0
    fi
    echo "== Архивы сессий (возврату не мешают, к сведению)"
    archives "$compose" || true
    echo
    echo "== Возврат прежнего сервера"
    said_by "new-container.log"
    put "$kept"
    bring_up || stop "прежний сервер не запустился. Руками: cd $home && docker compose up -d"
    well_in_time || stop "прежний сервер не стал здоровым. Журнал: cd $home && docker compose logs"
    read -r image status health <<< "$(container)"
    [ "$image" = "$was_id" ] || stop "контейнер работает не на прежнем образе $was"
    echo
    echo "ИТОГ ВОЗВРАТА: прежний сервер работает на $was и здоров"
    exit 0
fi

# ------------------------------------------------- what is there, what changes

ours_id="$(id_of "$IMAGE")"
[ -n "$ours_id" ] ||
    stop "нашего образа нет на диске: $IMAGE. Сперва сборка: ./piqnyx/build.sh"

rm -f "$new"
edited=0
python3 "$HERE/edit_compose.py" --file "$compose" --service "$SERVICE" \
    --image "$IMAGE" --was-of "${BASE%%@*}" --was-of "$NAME" --out "$new" > "$work/edit.txt" ||
    edited=$?

if [ "$edited" = "3" ]; then
    read -r image status health <<< "$(container)"
    [ "${image:-}" = "$ours_id" ] ||
        stop "в compose-файле уже наш образ, а контейнер работает не на нём. Поднять: cd $home && docker compose up -d"
    echo "контейнер $CONTAINER: образ $IMAGE, состояние $status, здоровье $health"
    echo "ИТОГ ЗАМЕНЫ: менять нечего, контейнер уже работает на $IMAGE"
    exit 0
fi
if [ "$edited" != "0" ]; then
    cat "$work/edit.txt"
    stop "compose-файл менять нельзя: $compose"
fi

was="$(python3 "$HERE/edit_compose.py" --file "$compose" --service "$SERVICE" --image-now)"
was_id="$(id_of "$was")"
[ -n "$was_id" ] || stop "образа из compose-файла нет на диске: $was"
# An earlier image of ours in the file: the second build takes the place of the first.
ours_before=0
case "$was" in
    "$NAME":*) ours_before=1 ;;
esac
if [ "$ours_before" = "1" ]; then
    echo "в compose-файле наш прежний образ: $was"
else
    [ "$was_id" = "$(id_of "$BASE")" ] ||
        stop "образ в compose-файле не тот, на котором собран наш: $was"
fi

state="$(container)"
[ -n "$state" ] || stop "контейнера $CONTAINER нет: заменять нечего"
read -r image status health <<< "$state"
[ "$image" = "$was_id" ] ||
    stop "контейнер $CONTAINER работает не на том образе, что записан в compose-файле"
[ "$status" = "running" ] && [ "$health" = "healthy" ] ||
    stop "сервер сейчас нездоров (состояние $status, здоровье $health): сперва разобраться с ним"

echo "== В compose-файле поменяется: $compose"
cat "$work/edit.txt"
echo
echo "== Архивы сессий"
blocked=0
archives "$compose" || blocked=1

if [ "$mode" = "show" ]; then
    echo
    if [ "$blocked" = "0" ]; then
        echo "ИТОГ: ничего не тронуто. Заменить: ./piqnyx/replace-container.sh --do"
        exit 0
    fi
    echo "ИТОГ: ничего не тронуто. Сейчас заменять нельзя"
    exit 1
fi

# ------------------------------------------------------------ the replacement

if [ "$blocked" != "0" ]; then
    echo
    echo "ИТОГ ЗАМЕНЫ: ОСТАНОВКА, ничего не тронуто"
    exit 1
fi

echo
echo "== Проверка образа перед заменой"
"$HERE/verify-image.sh" > "$work/verify.txt" 2>&1 || {
    cat "$work/verify.txt"
    echo
    echo "ИТОГ ЗАМЕНЫ: ОСТАНОВКА, образ не прошёл проверку, ничего не тронуто"
    exit 1
}
tail -n 1 "$work/verify.txt"

if [ -e "$kept" ] && ! cmp -s "$kept" "$compose"; then
    if [ "$ours_before" = "1" ]; then
        # The copy from the replacement before this one: put aside, numbered, so the
        # kept copy is always the file before the latest replacement.
        n=1
        while [ -e "$kept.$n" ]; do n=$((n + 1)); done
        mv "$kept" "$kept.$n"
        echo "копия от прошлой замены отложена: $kept.$n"
    else
        stop "копия от прошлого раза есть, и она другая: $kept. Сперва разобраться, какой файл верный"
    fi
fi
[ -e "$kept" ] || cp -p "$compose" "$kept"

in_compose "$new" config -q ||
    stop "docker не принял новый compose-файл, прежний не тронут: $new"

echo
echo "== Замена"
said_by "old-container.log"
put "$new"

risen=1
why=""
if bring_up; then
    if well_in_time; then
        read -r image status health <<< "$(container)"
        inside="$(docker exec "$CONTAINER" cat /app/PIQNYX-VERSION 2> /dev/null || true)"
        echo "  версия внутри: ${inside:-не узнать}"
        if [ "$image" = "$ours_id" ] && [ "$inside" = "$VERSION" ]; then
            risen=0
        else
            why="контейнер поднялся не на нашем образе"
        fi
    else
        why="новый сервер не поднялся"
    fi
else
    why="новый сервер не запустился"
fi

if [ "$risen" = "0" ]; then
    echo
    echo "копия прежнего compose-файла: $kept"
    echo "журнал прежнего контейнера: $work/old-container.log"
    echo "вернуть прежний: ./piqnyx/replace-container.sh --back"
    echo "ИТОГ ЗАМЕНЫ: контейнер $CONTAINER работает на $IMAGE и здоров"
    exit 0
fi

echo
echo "== $why, возвращаю прежний"
said_by "new-container.log"
put "$kept"
if bring_up && well_in_time; then
    echo
    echo "журнал нового контейнера: $work/new-container.log"
    echo "ЗАМЕНА НЕ УДАЛАСЬ: $why, прежний сервер возвращён и здоров"
    exit 1
fi
echo
echo "compose-файл прежний. Руками: cd $home && docker compose down && docker compose up -d"
echo "ЗАМЕНА НЕ УДАЛАСЬ, И ПРЕЖНИЙ СЕРВЕР НЕ ПОДНЯЛСЯ: $why"
exit 1
