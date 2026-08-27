param(
    [Parameter(Mandatory=$true)][string]$input_dir
)

$ErrorActionPreference = 'Stop'

# ---- settings from config.ini (next to this script) ---------------------
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Read-IniSettings($path) {
    $h = @{}
    if (-not (Test-Path -LiteralPath $path)) { return $h }
    foreach ($line in Get-Content -LiteralPath $path) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#') -or $t.StartsWith(';') -or $t.StartsWith('[')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $h[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
    }
    return $h
}
function IniStr($cfg, $key, $def) { if ($cfg.ContainsKey($key) -and $cfg[$key] -ne '') { $cfg[$key] } else { $def } }
function IniInt($cfg, $key, $def) { $v = IniStr $cfg $key $null; if ($null -ne $v) { try { [int]$v } catch { $def } } else { $def } }

$cfg = Read-IniSettings (Join-Path $scriptDir 'config.ini')
$paths = Read-IniSettings (Join-Path (Split-Path $scriptDir -Parent) 'Shared\paths.ini')
$irfanview       = IniStr $paths 'irfanview' 'C:\Program Files\IrfanView\i_view64.exe'
$renders_size    = IniInt $cfg 'renders_size' 1024
$thumbnails_size = IniInt $cfg 'thumbnails_size' 256
$quality         = IniInt $cfg 'quality' 75
$method          = IniInt $cfg 'method' 4
$passes          = IniInt $cfg 'passes' 1
$lossless        = IniInt $cfg 'lossless' 0
$workers         = IniInt $cfg 'workers' -1

$output_dir      = Join-Path $scriptDir 'output'
$renders_name    = 'renders'
$thumbnails_name = 'thumbnails'

# IrfanView fallback: 32-bit install if the configured path is absent.
if (-not (Test-Path -LiteralPath $irfanview)) {
    $alt = 'C:\Program Files (x86)\IrfanView\i_view32.exe'
    if (Test-Path -LiteralPath $alt) { $irfanview = $alt }
}

if ($workers -le 0) { $workers = [Environment]::ProcessorCount }

# Add ID for MessageBox and DPI awareness
Add-Type -AssemblyName System.Windows.Forms
$dpiSignature = @"
[DllImport("user32.dll")]
public static extern bool SetProcessDPIAware();
"@
Add-Type -MemberDefinition $dpiSignature -Name "DPIUtils" -Namespace "Win32"

$input_dir  = [System.IO.Path]::GetFullPath($input_dir).TrimEnd('\','/')
$output_dir = [System.IO.Path]::GetFullPath($output_dir).TrimEnd('\','/')

if (-not (Test-Path -LiteralPath $input_dir)) { Write-Host "Input folder not found: $input_dir" -ForegroundColor Red; exit 1 }
if (-not (Test-Path -LiteralPath $irfanview)) { Write-Host "Missing prerequisite: IrfanView not found at '$irfanview' (edit Shared\paths.ini; see the README)." -ForegroundColor Red; exit 1 }

$profiles = @(
    @{ name = $renders_name;    size = $renders_size },
    @{ name = $thumbnails_name; size = $thumbnails_size }
)

# Folders that directly contain PNGs (root + every subfolder).
$allDirs = @($input_dir) + @(Get-ChildItem -LiteralPath $input_dir -Directory -Recurse | ForEach-Object { $_.FullName })
$pngFolders = @($allDirs | Where-Object { @(Get-ChildItem -LiteralPath $_ -Filter *.png -File -ErrorAction SilentlyContinue).Count -gt 0 })
if ($pngFolders.Count -eq 0) { Write-Host "No PNG files found under $input_dir" -ForegroundColor Yellow; exit 0 }

$pngTotal = ($pngFolders | ForEach-Object { @(Get-ChildItem -LiteralPath $_ -Filter *.png -File).Count } | Measure-Object -Sum).Sum

# One job per (folder x profile); each is a single IrfanView wildcard convert.
$jobs = New-Object System.Collections.ArrayList
foreach ($f in $pngFolders) {
    $rel = $f.Substring($input_dir.Length)   # '' for root, else '\meshes\l'
    foreach ($p in $profiles) {
        $destDir = (Join-Path $output_dir $p.name) + $rel
        [void]$jobs.Add([pscustomobject]@{ Src = $f; DestDir = $destDir; Size = $p.size })
    }
}
$total = $jobs.Count
Write-Host ("Folders: {0}   PNGs: {1}   Jobs: {2}   Workers: {3}" -f $pngFolders.Count, $pngTotal, $total, $workers)

$iniText = @"
[WEBP]
SaveOption=$lossless
SaveQuality=$quality
Method=$method
Passes=$passes
SavePreset=0
SaveFilter=0
SaveFilterStrength=60
SaveSharpness=0
SaveSharpnessValue=0
"@

# One ini folder per worker slot -> only ever one IrfanView per folder = no ini write contention.
$iniRoot = Join-Path $env:TEMP ('webp_par_' + [guid]::NewGuid().ToString('N').Substring(0,8))
$iniFolders = @()
for ($w = 0; $w -lt $workers; $w++) {
    $wf = Join-Path $iniRoot ("w$w")
    New-Item -ItemType Directory -Path $wf -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $wf 'i_view64.ini') -Value $iniText -Encoding Ascii
    $iniFolders += $wf
}

$sb = {
    param($job, $irfanview, $iniFolder)
    if (-not (Test-Path -LiteralPath $job.DestDir)) { New-Item -ItemType Directory -Path $job.DestDir -Force | Out-Null }
    # /silent: report load/save errors through the exit code instead of a modal
    # dialog, which -WindowStyle Hidden does not suppress and which would block
    # the worker until someone clicks OK.
    $argStr = '"' + $job.Src + '\*.png" /resize=(' + $job.Size + ',' + $job.Size + ') /aspectratio /resample /silent /ini="' + $iniFolder + '" /convert="' + $job.DestDir + '\*.webp"'
    try {
        $proc = Start-Process -FilePath $irfanview -ArgumentList $argStr -Wait -PassThru -WindowStyle Hidden
        return $proc.ExitCode        # 0 = ok, 1/2 = load or save error
    } catch {
        return -1                    # could not launch IrfanView at all
    }
}

$sw = [System.Diagnostics.Stopwatch]::StartNew()

$pool = [runspacefactory]::CreateRunspacePool(1, $workers)
$pool.Open()

$queue = New-Object System.Collections.Queue
foreach ($j in $jobs) { $queue.Enqueue($j) }
$running = New-Object System.Collections.ArrayList

function Start-One($job, $iniIdx) {
    $ps = [powershell]::Create()
    $ps.RunspacePool = $pool
    [void]$ps.AddScript($sb).AddArgument($job).AddArgument($irfanview).AddArgument($iniFolders[$iniIdx])
    return [pscustomobject]@{ PS = $ps; Handle = $ps.BeginInvoke(); IniIdx = $iniIdx; Job = $job }
}

for ($k = 0; $k -lt $workers -and $queue.Count -gt 0; $k++) { [void]$running.Add((Start-One $queue.Dequeue() $k)) }

$done = 0
$failedJobs = New-Object System.Collections.ArrayList
while ($running.Count -gt 0) {
    for ($idx = $running.Count - 1; $idx -ge 0; $idx--) {
        $r = $running[$idx]
        if ($r.Handle.IsCompleted) {
            $code = -1
            try {
                $out = @($r.PS.EndInvoke($r.Handle))
                if ($out.Count -gt 0) { $code = [int]$out[-1] }
            } catch { $code = -1 }
            $r.PS.Dispose()
            if ($code -ne 0) {
                [void]$failedJobs.Add([pscustomobject]@{ Src = $r.Job.Src; Size = $r.Job.Size; ExitCode = $code })
            }
            $iniIdx = $r.IniIdx
            $running.RemoveAt($idx)
            $done++
            Write-Host -NoNewline ("`r  [{0}/{1}]" -f $done, $total) -ForegroundColor DarkGray
            if ($queue.Count -gt 0) { [void]$running.Add((Start-One $queue.Dequeue() $iniIdx)) }
        }
    }
    Start-Sleep -Milliseconds 50
}
Write-Host ""

$pool.Close(); $pool.Dispose()
Remove-Item -LiteralPath $iniRoot -Recurse -Force -ErrorAction SilentlyContinue
$sw.Stop()

# Verify name by name, per folder. A plain recursive count would let leftovers
# from an earlier run stand in for files this run failed to write.
$missing = New-Object System.Collections.ArrayList
foreach ($j in $jobs) {
    $have = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    if (Test-Path -LiteralPath $j.DestDir) {
        foreach ($w in Get-ChildItem -LiteralPath $j.DestDir -Filter *.webp -File) { [void]$have.Add($w.BaseName) }
    }
    foreach ($src in Get-ChildItem -LiteralPath $j.Src -Filter *.png -File) {
        if (-not $have.Contains($src.BaseName)) {
            $miss = Join-Path $j.DestDir ($src.BaseName + '.webp')
            [void]$missing.Add($miss.Substring($output_dir.Length + 1))
        }
    }
}

$expected = $pngTotal * $profiles.Count
$made     = $expected - $missing.Count

# ---- log ----------------------------------------------------------------
$logPath = Join-Path $output_dir 'build_gallery_images.log'
$log = New-Object System.Collections.ArrayList
[void]$log.Add(("Build Gallery Images - {0}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')))
[void]$log.Add(("Input    : {0}" -f $input_dir))
[void]$log.Add(("Output   : {0}" -f $output_dir))
[void]$log.Add(("Settings : renders={0} thumbnails={1} quality={2} method={3} passes={4} lossless={5} workers={6}" -f $renders_size, $thumbnails_size, $quality, $method, $passes, $lossless, $workers))
[void]$log.Add(("Result   : {0} folders, {1} PNGs, {2} jobs, {3}/{4} webp written in {5:n1}s" -f $pngFolders.Count, $pngTotal, $total, $made, $expected, $sw.Elapsed.TotalSeconds))
[void]$log.Add('')

if ($failedJobs.Count -gt 0) {
    [void]$log.Add(("IrfanView reported an error in {0} job(s)  (exit 1/2 = load or save error, -1 = could not launch):" -f $failedJobs.Count))
    foreach ($f in $failedJobs) {
        $rel = if ($f.Src.Length -gt $input_dir.Length) { $f.Src.Substring($input_dir.Length + 1) } else { '.' }
        [void]$log.Add(("  exit {0}  size {1}  {2}" -f $f.ExitCode, $f.Size, $rel))
    }
    [void]$log.Add('')
}

if ($missing.Count -gt 0) {
    [void]$log.Add(("Missing {0} output file(s):" -f $missing.Count))
    foreach ($m in $missing) { [void]$log.Add("  $m") }
} else {
    [void]$log.Add('No missing output files.')
}

if (-not (Test-Path -LiteralPath $output_dir)) { New-Item -ItemType Directory -Path $output_dir -Force | Out-Null }
Set-Content -LiteralPath $logPath -Value $log -Encoding UTF8

Write-Host ("Elapsed: {0:n1}s" -f $sw.Elapsed.TotalSeconds)
Write-Host ("Log: {0}" -f $logPath) -ForegroundColor DarkGray

try { [void][Win32.DPIUtils]::SetProcessDPIAware() } catch { }

if ($missing.Count -gt 0 -or $failedJobs.Count -gt 0) {
    Write-Host ("WARNING: expected {0} webp files, found {1}." -f $expected, $made) -ForegroundColor Yellow
    foreach ($m in @($missing | Select-Object -First 10)) { Write-Host ("  missing: {0}" -f $m) -ForegroundColor Yellow }
    if ($missing.Count -gt 10) { Write-Host ("  ...and {0} more, see the log." -f ($missing.Count - 10)) -ForegroundColor Yellow }
    [void][System.Windows.Forms.MessageBox]::Show(
        ("WEBP conversion finished with problems.`n`nExpected {0} files, found {1}.`n`nDetails: {2}" -f $expected, $made, $logPath),
        "Build Gallery Images",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning)
    exit 1
}

Write-Host ("OK: {0} webp files." -f $made) -ForegroundColor Green
[void][System.Windows.Forms.MessageBox]::Show(
    ("{0} WEBP files written to the output folder." -f $made),
    "Build Gallery Images",
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Information)

# Open the output folder
if (Test-Path $output_dir) { Invoke-Item $output_dir }
exit 0
