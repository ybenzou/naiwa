param([string]$PythonExe, [string]$DataDir, [string]$HookEvent)
# Cursor's Windows host uses Get-Content -Raw without Encoding, which can destroy
# UTF-8 JSON. The invocation gives the exact current payload file; never scan
# other files, transcripts, IDE logs or databases to identify a conversation.
$ErrorActionPreference = 'Stop'
try {
    $hookArgs = @('-m', 'naiwa.hook')
    $hookArgs += @('--source', 'cursor', '--data-dir', $DataDir, '--event', $HookEvent)
    $invocationMatch = [regex]::Match($MyInvocation.Line, "Get-Content\s+-LiteralPath\s+'((?:[^']|'')*)'\s+-Raw")
    $payloadPath = $null
    if ($invocationMatch.Success) {
        $candidatePath = [IO.Path]::GetFullPath($invocationMatch.Groups[1].Value.Replace("''", "'"))
        $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if ($candidatePath.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFileName($candidatePath).StartsWith('cursor-hook-payload-')) {
            $payloadPath = $candidatePath
        }
    }
    if ($payloadPath) {
        & $PythonExe @hookArgs --input-file $payloadPath
    } elseif ($MyInvocation.ExpectingInput) {
        $OutputEncoding = [Text.Encoding]::UTF8
        $input | & $PythonExe @hookArgs
    } else {
        & $PythonExe @hookArgs
    }
} catch {
    if ($HookEvent -eq 'beforeSubmitPrompt') { [Console]::WriteLine('{"continue":true}') }
    elseif ($HookEvent -in @('preToolUse', 'postToolUse', 'subagentStart')) { [Console]::WriteLine('{"permission":"allow"}') }
    else { [Console]::WriteLine('{}') }
}
exit 0
