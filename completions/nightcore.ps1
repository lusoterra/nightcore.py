# PowerShell completion for nightcore (Windows PowerShell 5.1 and PowerShell 7+).
#
# The setup installer drops this file at:
#   $env:ProgramFiles\nightcore\completions\nightcore.ps1
# Load it from your profile (once):
#   . "$env:ProgramFiles\nightcore\completions\nightcore.ps1"
# Portable/bundle users dot-source the copy next to the exe instead:
#   . "<dir>\completions\nightcore.ps1"   (setup/portable tree)
#   . "<dir>\nightcore.ps1"               (sidecar next to the bundle exe)
#
# Completes flags, option values (--format, speeds, ...), directories
# for --batch/--stats/--output-folder, files for -o/--tag-template/--cover,
# and audio files (.flac/.mp3/.../.lrc) for the input argument.

Register-ArgumentCompleter -Native -CommandName @('nightcore', 'nightcore-bundle') -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    $flags = @(
        '-s', '--speed', '-r', '--reciprocal', '-y', '--overwrite',
        '-q', '--quiet', '-R', '--resample', '-F', '--format',
        '--lossy-opts=', '--batch', '--stats', '--recurse',
        '-p', '--pitch', '-S', '--semitones', '-n', '--dry-run',
        '--autorename', '--no-delete-tags', '--no-stamp', '--no-pipeline',
        '-o', '--output', '--output-folder', '--in-place',
        '-N', '--renametag', '--tag-template', '--cover', '--mkvid',
        '--pitch-diff', '--speed-diff', '-D', '--denightcore',
        '--version', '--check', '-h', '--help'
    )

    # Options taking a fixed set of values.
    $valueOptions = @{
        '-F' = $formats; '--format' = $formats
        '--recurse' = $depths
    }
    # Speeds/semitones take any float: free-form, no guessing.
    $dirOptions = @('--batch', '--stats', '--output-folder')
    $fileOptions = @('-o', '--output', '--tag-template', '--cover')
    # Free-form values: stay out of the way (like the other shells).
    $freeOptions = @('-s', '--speed', '-S', '--semitones', '--autorename', '--lossy-opts', '--lossy-opts=', '--pitch-diff', '--speed-diff')

    $audioExts = @(
        '.flac', '.wav', '.aiff', '.aif', '.ape', '.wv', '.tta',
        '.alac', '.m4a', '.mp3', '.ogg', '.oga', '.opus', '.aac',
        '.wma', '.mpc', '.ac3', '.dts', '.amr', '.speex', '.mp2', '.lrc'
    )

    function Complete-Values($candidates, $prefix, $resultType) {
        foreach ($c in $candidates) {
            if ($c -like "$prefix*") {
                [System.Management.Automation.CompletionResult]::new(
                    $c, $c, $resultType, $c)
            }
        }
    }

    function Complete-Path($dirsOnly, $extensions) {
        $pattern = if ($wordToComplete) { "$wordToComplete*" } else { '*' }
        try {
            $items = Get-ChildItem -Path $pattern -ErrorAction Stop
        } catch {
            return
        }
        foreach ($item in $items) {
            if ($dirsOnly -and -not $item.PSIsContainer) { continue }
            if (-not $dirsOnly -and -not $item.PSIsContainer -and $extensions `
                    -and $audioExts -notcontains $item.Extension.ToLower()) { continue }
            $type = if ($item.PSIsContainer) { 'ProviderContainer' } else { 'ProviderItem' }
            [System.Management.Automation.CompletionResult]::new(
                $item.FullName, $item.Name, $type, $item.FullName)
        }
    }

    # Joined --opt=value form (only --lossy-opts= is accepted by the tool,
    # and it is free-form, so anything joined just gets out of the way;
    # --format= is completed for convenience).
    if ($wordToComplete -match '^(--[^=]+)=(.*)$') {
        $opt, $valPrefix = $Matches[1], $Matches[2]
        if ($opt -eq '--format') {
            foreach ($c in $formats) {
                if ($c -like "$valPrefix*") {
                    $full = "$opt=$c"
                    [System.Management.Automation.CompletionResult]::new(
                        $full, $full, 'ParameterValue', $full)
                }
            }
        }
        return
    }

    $elements = @($commandAst.CommandElements | ForEach-Object { $_.Extent.Text })
    $prev = if ($elements.Count -ge 2) {
        $elements[$elements.Count - 2].Trim('"', "'")
    } else {
        ''
    }

    if ($prev -and $valueOptions.ContainsKey($prev)) {
        Complete-Values $valueOptions[$prev] $wordToComplete 'ParameterValue'
        return
    }
    if ($prev -and $dirOptions -contains $prev) {
        Complete-Path -dirsOnly $true
        return
    }
    if ($prev -and $fileOptions -contains $prev) {
        if ($prev -eq '--cover') {
            $pattern = if ($wordToComplete) { "$wordToComplete*" } else { '*' }
            try {
                Get-ChildItem -Path $pattern -File -ErrorAction Stop |
                    Where-Object { @('.png', '.jpg', '.jpeg') -contains $_.Extension.ToLower() } |
                    ForEach-Object {
                        [System.Management.Automation.CompletionResult]::new(
                            $_.FullName, $_.Name, 'ProviderItem', $_.FullName)
                    }
            } catch {
            }
            return
        }
        Complete-Path -dirsOnly $false
        return
    }
    if ($prev -and $freeOptions -contains $prev) {
        return
    }

    if ($wordToComplete -like '-*') {
        Complete-Values $flags $wordToComplete 'ParameterName'
        return
    }

    # Positional input: audio files only.
    Complete-Path -dirsOnly $false -extensions $audioExts
}
