#!/usr/bin/env bash
# Is the branching clean? One command, run after anything that merges.
#
#   scripts/branch-audit.sh          # report
#   scripts/branch-audit.sh --prune  # and delete what is safe to delete
#
# The strategy this checks (see DEPLOYING.md):
#
#   main       the trunk. Everything arrives by pull request.
#   claude/*   one branch per piece of work, one pull request each. Deleted
#              once merged — a merged branch is history twice over, and the
#              pile of them is what makes it hard to see which branch is
#              the live one.
#   release    a POINTER, not a line of development: what the Pi runs. It
#              is ahead of main whenever work is deployed before its pull
#              request lands, which is normal here — the Pi is the test.
#
# What counts as orphaned: a claude/* branch fully contained in main. Its
# commits are in the trunk, so deleting it loses nothing. Anything NOT
# contained in main is left alone and reported, because unmerged work is
# not the script's to throw away.
set -uo pipefail

PRUNE=0
[[ "${1:-}" == "--prune" ]] && PRUNE=1

git fetch --quiet --prune origin || {
    echo "cannot reach origin" >&2; exit 1; }

TRUNK="origin/main"
current="$(git rev-parse --abbrev-ref HEAD)"
merged=() unmerged=()

while read -r ref; do
    short="${ref#origin/}"
    case "$short" in
        main|release|HEAD) continue ;;
    esac
    if git merge-base --is-ancestor "$ref" "$TRUNK"; then
        merged+=("$short")
    else
        unmerged+=("$short")
    fi
done < <(git for-each-ref --format='%(refname:short)' refs/remotes/origin)

echo "trunk:   main    $(git rev-parse --short $TRUNK)"
echo "deploy:  release $(git rev-parse --short origin/release)  ($(git rev-list --count $TRUNK..origin/release) ahead of main)"
echo

if ((${#merged[@]})); then
    echo "ORPHANED — merged into main, safe to delete (${#merged[@]}):"
    printf '  %s\n' "${merged[@]}"
    if ((PRUNE)); then
        echo
        for b in "${merged[@]}"; do
            if git push --quiet origin --delete "$b" 2>/dev/null; then
                echo "  deleted $b"
            else
                # The credential a session runs under may create and update
                # refs without being allowed to delete them. Saying which it
                # was beats a bare failure.
                echo "  COULD NOT DELETE $b — this credential cannot delete refs;"
                echo "     delete it on GitHub, or run this from a checkout that can."
            fi
            git branch -D "$b" >/dev/null 2>&1 && echo "     (local copy removed)"
        done
    else
        echo "  run with --prune to delete them"
    fi
else
    echo "no orphaned branches"
fi

echo
if ((${#unmerged[@]})); then
    echo "LIVE — not in main, left alone (${#unmerged[@]}):"
    for b in "${unmerged[@]}"; do
        ahead="$(git rev-list --count "$TRUNK..origin/$b")"
        here=""
        [[ "$b" == "$current" ]] && here="   <- checked out here"
        printf '  %-46s %s commit(s) ahead%s\n' "$b" "$ahead" "$here"
    done
    echo
    echo "  Each should have an open pull request. One that does not is work"
    echo "  nobody is going to merge — finish it or close it deliberately."
fi
