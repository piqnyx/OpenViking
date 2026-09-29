#!/bin/sh
# piqnyx: our files go over the image one by one, and nothing else is touched.
# SPDX-License-Identifier: AGPL-3.0
#
# The step of the build that lays the files of piqnyx/overlay.txt over the
# site-packages of the image (PIQNYX.md, stage 2). piqnyx/Dockerfile runs it:
#
#     lay_over.sh CHECKOUT SITE VERSION [NOTES]
#
# `python` is the one the image finds first, the one the server runs on.
# Everything is looked at before anything is touched.
set -eu
umask 022

stop() {
    echo "ОСТАНОВКА: $*" >&2
    exit 1
}

[ "$#" -ge 3 ] || stop "нужны каталог исходников, каталог пакетов и версия сборки"
src=$1
site=$2
version=$3
notes=${4:-/app}
list="$src/piqnyx/overlay.txt"

[ -n "$version" ] || stop "не дана версия сборки"
[ -d "$site" ] || stop "нет каталога пакетов: $site"
[ -d "$notes" ] || stop "нет каталога для записок: $notes"
[ -f "$list" ] || stop "нет списка наших файлов: $list"

count=0
while IFS= read -r name || [ -n "$name" ]; do
    [ -n "$name" ] || continue
    case "/$name/" in
        *//* | */../* | */./*) stop "имя ведёт вон из пакетов: $name" ;;
    esac
    [ -f "$src/$name" ] || stop "в исходниках нет файла из списка: $name"
    case "$name" in
        *.py)
            python -B -c 'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' \
                "$src/$name" || stop "файл не компилируется: $name"
            ;;
    esac
    count=$((count + 1))
done < "$list"
[ "$count" -gt 0 ] || stop "список наших файлов пуст: $list"

# What we replace keeps the mode it had in the image; what is new is given 0644.
mode_of() {
    if [ -f "$1" ]; then stat -c '%a' "$1"; else echo 0644; fi
}

while IFS= read -r name || [ -n "$name" ]; do
    [ -n "$name" ] || continue
    mode=$(mode_of "$site/$name")
    install -D -m "$mode" "$src/$name" "$site/$name"
    case "$name" in
        *.py)
            compiled=$(python -B -c \
                'import importlib.util, sys; print(importlib.util.cache_from_source(sys.argv[1]))' \
                "$site/$name")
            mode=$(mode_of "$compiled")
            # -B: no bytecode is written but the one asked for here.
            python -B -c 'import py_compile, sys; py_compile.compile(sys.argv[1], doraise=True)' \
                "$site/$name"
            chmod "$mode" "$compiled"
            ;;
    esac
done < "$list"

install -m 0644 "$list" "$notes/PIQNYX-OVERLAY.txt"
printf '%s\n' "$version" > "$notes/PIQNYX-VERSION"
chmod 0644 "$notes/PIQNYX-VERSION"
echo "наложено файлов: $count, версия сборки $version"
