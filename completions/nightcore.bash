# bash completion for nightcore
_nightcore() {
    local cur prev words cword
    _init_completion -n : 2>/dev/null || {
        COMPREPLY=()
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev="${COMP_WORDS[COMP_CWORD-1]}"
        words=("${COMP_WORDS[@]}")
        cword=$COMP_CWORD
    }

    local opts="-s --speed -r --reciprocal -y --overwrite -q --quiet -R --resample -F --format --lossy-opts= --batch -p --pitch -S --semitones --pitch-diff --speed-diff -D --denightcore -n --dry-run --simple-title --autotag --autorename -N --renametag --in-place --no-delete-tags -o --output --output-folder --tag-template --cover -h --help"

    case "$prev" in
        -s|--speed)
            # Common nightcore speed factors
            COMPREPLY=($(compgen -W "1.1 1.15 1.2 1.25 1.3 0.9 0.85 0.8" -- "$cur"))
            return 0
            ;;
        -S|--semitones)
            # Semitone equivalents of common speed factors
            COMPREPLY=($(compgen -W "1.65 2.41 3.16 3.86 4.55 -1.82 -2.80 -3.86" -- "$cur"))
            return 0
            ;;
        -F|--format)
            COMPREPLY=($(compgen -W "flac alac wav mp3 opus aac" -- "$cur"))
            return 0
            ;;
        --tag-template)
            COMPREPLY=($(compgen -f -- "$cur"))
            return 0
            ;;
        --cover)
            _filedir '@(png|jpg|jpeg|PNG|JPG|JPEG)'
            return 0
            ;;
        --lossy-opts|--lossy-opts=*)
            # Free-form encoder flags: no completion
            # (= form only: space-separated values are rejected)
            return 0
            ;;
        --simple-title)
            # Free-form title suffix: no completion
            return 0
            ;;
        =)
            # Bash splits --opt=value at '=': stay out of the value.
            case "${words[cword-2]:-}" in
                --lossy-opts)
                    return 0
                    ;;
            esac
            ;;
        --batch)
            COMPREPLY=($(compgen -d -- "$cur"))
            return 0
            ;;
    esac

    case "$cur" in
        --lossy-opts=*)
            # Unsplittable --opt="..." form: free-form value, no completion.
            return 0
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$opts" -- "$cur"))
        if ((${#COMPREPLY[@]} == 1)) && [[ ${COMPREPLY[0]} == *= ]]; then
            # Complete --opt= with no trailing space, so the value
            # starts right after '=' (the space form does not work
            # for values starting with '-').
            compopt +o nospace 2>/dev/null || true
        fi
        return 0
    fi

    # Complete audio files for the positional input argument
    _filedir '@(flac|wav|aiff|aif|ape|wv|tta|alac|m4a|mp3|ogg|oga|opus|aac|wma|mpc|ac3|dts|amr|speex|mp2|lrc)'
}
complete -F _nightcore nightcore
complete -F _nightcore nightcore.py
