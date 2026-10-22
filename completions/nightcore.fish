# fish completion for nightcore
# Install: cp nightcore.fish ~/.config/fish/completions/

complete -c nightcore -s s -l speed -d 'Speed factor (1.2 = 20% faster)' -x -a '1.1 1.15 1.2 1.25 1.3 0.9 0.85 0.8'
complete -c nightcore -s r -l reciprocal -d 'Use 1/speed instead (undo a speedup)'
complete -c nightcore -s y -l overwrite -d 'Overwrite existing output files'
complete -c nightcore -s q -l quiet -d 'Drop tool output (repeat for warnings/errors only)'
complete -c nightcore -s R -l resample -d 'Resample to next standard rate via SoX'
complete -c nightcore -s F -l format -d 'Output format (mkv/mp4/mp4alac need --mkvid)' -x -a 'flac alac mp3 opus aac mkv mp4 mp4alac'
complete -c nightcore -l lossy-opts -d 'Encoder quality flags (equals form only)' -r
complete -c nightcore -l batch -d 'Convert every audio file in DIR' -r -a '(__fish_complete_directories)'
complete -c nightcore -l stats -d 'Print statistics for DIR and exit' -r -a '(__fish_complete_directories)'
complete -c nightcore -l recurse -d 'Descend into subdirectories for --stats' -x -a '2 3 4'
complete -c nightcore -s p -l pitch -d 'Pitch-shift only, keep duration (needs rubberband)'
complete -c nightcore -s S -l semitones -d 'Pitch-shift by semitones' -x -a '1.65 2.41 3.16 3.86 4.55 -1.82 -2.80 -3.86'
complete -c nightcore -s n -l dry-run -d 'Print output names, convert nothing'
complete -c nightcore -l autorename -d 'Rename output via filename template (bare = default template)' -r
complete -c nightcore -l no-delete-tags -d 'With --tag-template: keep unlisted source tags'
complete -c nightcore -l no-stamp -d 'Skip the DESCRIPTION provenance tag'
complete -c nightcore -l no-pipeline -d 'Skip the encoder_settings pipeline tag'
complete -c nightcore -s o -l output -d 'Override the output filename' -r
complete -c nightcore -l output-folder -d 'Base for batch folders' -r -a '(__fish_complete_directories)'
complete -c nightcore -l in-place -d 'With -N: renametag alongside the inputs'
complete -c nightcore -s N -l renametag -d 'Renametag mode: rewrite tags and rename via stream copy'
complete -c nightcore -l tag-template -d 'Tag template file' -r
complete -c nightcore -l cover -d 'PNG/JPG artwork' -r
complete -c nightcore -l mkvid -d 'Remux song (+lyrics/image) into a 1080p YouTube video'
complete -c nightcore -l pitch-diff -d 'Calculator: speed or frequencies to semitones' -x
complete -c nightcore -l speed-diff -d 'Calculator: semitones to speed factor' -x
complete -c nightcore -s D -l denightcore -d 'Undo from the stamp tag only'
complete -c nightcore -l version -d 'Print version and exit'
complete -c nightcore -l check -d 'Check external tools and exit'
