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

    local opts="-s --speed -r --reciprocal -R --resample --target-rate -B --both -F --format -y --overwrite -h --help"

    case "$prev" in
        -s|--speed)
            # Common nightcore speed factors
            COMPREPLY=($(compgen -W "1.1 1.15 1.2 1.25 1.3 0.9 0.85 0.8" -- "$cur"))
            return 0
            ;;
        --target-rate)
            COMPREPLY=($(compgen -W "44100 48000 88200 96000 176400 192000" -- "$cur"))
            return 0
            ;;
        -F|--format)
            COMPREPLY=($(compgen -W "flac alac wav" -- "$cur"))
            return 0
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$opts" -- "$cur"))
        return 0
    fi

    # Complete audio files for the positional input argument
    _filedir '@(flac|wav|aiff|aif|ape|wv|tta|alac|m4a|mp3|ogg|oga|opus|aac|wma|mpc|ac3|dts|amr|speex|mp2)'
}
complete -F _nightcore nightcore
complete -F _nightcore nightcore.py
