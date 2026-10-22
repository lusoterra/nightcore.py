# POSIX sh word-list helper for nightcore.
#
# FreeBSD sh, OpenBSD ksh and busybox ash have no programmable completion
# API, so there is nothing to hook into: source this file from your
# shell rc and use the helpers below for manual lookup (grep, fzf, ...).
#
#   . /path/to/nightcore.sh
#   nightcore_words            # every flag, one per line
#   nightcore_values -F        # candidate values for -s/-S/-F/--recurse
#
# fish/zsh/bash users want completions/nightcore.fish,
# completions/_nightcore and completions/nightcore.bash instead.

nightcore_words() {
    printf '%s\n' \
        -s --speed \
        -r --reciprocal \
        -y --overwrite \
        -q --quiet \
        -R --resample \
        -F --format \
        --lossy-opts= \
        --batch \
        --stats \
        --recurse \
        -p --pitch \
        -S --semitones \
        -n --dry-run \
        --autorename \
        --no-delete-tags \
        --no-stamp \
        --no-pipeline \
        -o --output \
        --output-folder \
        --in-place \
        -N --renametag \
        --tag-template \
        --cover \
        --mkvid \
        --pitch-diff \
        --speed-diff \
        -D --denightcore \
        --version \
        --check \
        -h --help
}

nightcore_values() {
    case "$1" in
        -s|--speed) printf '%s\n' 1.1 1.15 1.2 1.25 1.3 0.9 0.85 0.8 ;;
        -S|--semitones) printf '%s\n' 1.65 2.41 3.16 3.86 4.55 -1.82 -2.80 -3.86 ;;
        -F|--format) printf '%s\n' flac alac mp3 opus aac mkv mp4 mp4alac ;;
        --recurse) printf '%s\n' 2 3 4 ;;
        *) return 1 ;;
    esac
}
