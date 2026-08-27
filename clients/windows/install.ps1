<#
    THIS FILE MUST STAY PURE ASCII.

    Windows PowerShell 5.1 reads a BOM-less script as ANSI, not UTF-8. A UTF-8
    em dash (E2 80 94) or box-drawing rule (E2 94 80) then decodes with byte
    0x94 as a smart quote, and PowerShell 5.1 treats smart quotes as string
    delimiters - so the script dies with "string missing terminator" and a
    cascade of unexpected-token errors, even when the offending character is
    inside a comment. The file is also written with a UTF-8 BOM as a second
    layer of protection. tests/test_windows_client.py enforces both.
#>
<#
.SYNOPSIS
    Install the Owner OS Windows agent: enroll this PC, register a workspace,
    and run the agent automatically at logon.

.DESCRIPTION
    One command plus a one-time enrollment code, as designed:

        powershell -ExecutionPolicy Bypass -File install.ps1 `
            -Server https://owner-os.example -Code OOS-XXXXX-XXXXX-XXXXX `
            -WorkspacePath "C:\Users\0962871647\Desktop\GAIKA_Basket_Chrome_Extension_MVP_v0.1.0\gaika-basket-extension"

    What it does, in order:
      1. checks Python 3.9+ and the Claude Code CLI are present (and says
         exactly what to install if not - it never silently installs software);
      2. copies owner_os_agent.py to %ProgramData%\OwnerOS;
      3. exchanges the one-time code for a per-device identity + secret, stored
         in %ProgramData%\OwnerOS\agent.json and locked down with icacls to
         SYSTEM, Administrators and the owner's own account;
      4. enrolls the workspace folder(s) you named - nothing outside them is
         ever reachable;
      5. registers a Scheduled Task that runs the agent at logon and restarts
         it if it stops.

    The agent makes OUTBOUND connections only. No inbound port is opened and no
    firewall rule is added; if this script ever asks you for one, something is
    wrong. -Server must be HTTPS, or a Tailscale address (*.ts.net or a
    100.64.0.0/10 tailnet IP), where WireGuard already provides the encryption
    and peer authentication TLS would otherwise add.

.NOTES
    Re-running is safe: enrollment is skipped when the device is already
    enrolled (use -Force to re-enroll with a fresh code), and the scheduled
    task is replaced rather than duplicated.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Server,
    [Parameter(Mandatory = $false)][string] $Code = "",
    [Parameter(Mandatory = $false)][string] $WorkspacePath = "",
    [Parameter(Mandatory = $false)][string] $WorkspaceId = "",
    [Parameter(Mandatory = $false)][string] $DeviceName = $env:COMPUTERNAME,
    [Parameter(Mandatory = $false)][string] $InstallDir = (Join-Path $env:ProgramData "OwnerOS"),
    [switch] $Force,
    [switch] $NoAutostart
)

$ErrorActionPreference = "Stop"

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Warn($text) { Write-Host "!!  $text" -ForegroundColor Yellow }

# -- 1. prerequisites --------------------------------------------------------
Write-Step "Checking prerequisites"

# Windows PowerShell 5.1 is still the default shell on Windows, so this script
# stays 5.1-compatible: no ?? operator, no ternaries, no PS7-only cmdlets.
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if ($python -and $python.Source -like "*WindowsApps*python*.exe") {
    # The Microsoft Store alias stub: it "exists" but only opens the Store.
    $real = & $python.Source -c "print(1)" 2>$null
    if (-not $real) { $python = $null }
}
if (-not $python) {
    throw "Python 3.9+ not found on PATH. Install it with:  winget install -e --id Python.Python.3.12   then re-run this script."
}
$pyVersion = & $python.Source -c "import sys;print('%d.%d' % sys.version_info[:2])"
if ([version]$pyVersion -lt [version]"3.9") {
    throw "Python $pyVersion is too old; 3.9+ is required."
}
Write-Host "    python $pyVersion at $($python.Source)"

$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    Write-Warn "Claude Code CLI not found on PATH. Install it with:  npm install -g @anthropic-ai/claude-code"
    Write-Warn "The agent will enroll now and start working once 'claude' is on PATH."
} else {
    Write-Host "    claude at $($claude.Source)"
}

# Transport check. HTTPS is the normal requirement, with ONE principled
# exception: a Tailscale address. Traffic to a *.ts.net MagicDNS name or a
# 100.64.0.0/10 tailnet IP is already inside a WireGuard tunnel that both
# encrypts it and authenticates the peer, so plain HTTP there is not clear
# text on any wire - it is the same guarantee TLS would add, provided one
# layer down. Plain HTTP to anything else is still refused.
$serverHost = ([uri]$Server).Host
$isTailnet = $false
if ($serverHost -like "*.ts.net") { $isTailnet = $true }
elseif ($serverHost -match '^100\.(6[4-9]|[7-9][0-9]|1[0-1][0-9]|12[0-7])\.') { $isTailnet = $true }

if ($Server -notmatch '^https://') {
    if ($isTailnet) {
        Write-Host "    transport: Tailscale ($serverHost) - WireGuard-encrypted, tailnet-only" -ForegroundColor Green
    } else {
        Write-Warn "Server URL is not HTTPS. The device secret and every command would travel in clear text."
        if (-not $Force) { throw "Refusing to install against a non-HTTPS, non-Tailscale server (use -Force only for a local test)." }
    }
}

if ($isTailnet) {
    $ts = Get-Command tailscale -ErrorAction SilentlyContinue
    if (-not $ts) {
        Write-Warn "Server is a Tailscale address but the tailscale CLI was not found on PATH."
        Write-Warn "Install Tailscale and sign in to the same tailnet:  winget install -e --id tailscale.tailscale"
    } else {
        Write-Step "Checking this machine can reach $serverHost over the tailnet"
        $reach = Test-NetConnection -ComputerName $serverHost -Port ([uri]$Server).Port -InformationLevel Quiet -WarningAction SilentlyContinue
        if (-not $reach) {
            throw "Cannot reach $serverHost on the tailnet. Bring Tailscale up on this PC (tailscale up) and retry - enrollment cannot work until the tailnet path is live."
        }
        Write-Host "    tailnet path OK"
    }
}

# -- 2. install files --------------------------------------------------------
Write-Step "Installing to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$agentSource = Join-Path $PSScriptRoot "owner_os_agent.py"
if (-not (Test-Path $agentSource)) { throw "owner_os_agent.py not found next to this script." }
$agentPath = Join-Path $InstallDir "owner_os_agent.py"
Copy-Item $agentSource $agentPath -Force

$configPath = Join-Path $InstallDir "agent.json"

# Lock the directory down BEFORE the secret is written into it: SYSTEM,
# Administrators, and the owner's own account - and NOBODY else. Inherited ACEs
# are dropped (/inheritance:r) so a permissive parent cannot leak it.
#
# The owner's account must keep access because the agent runs AS the owner (see
# the scheduled-task principal below) and has to read this file. That is not a
# weakening: the same account already holds the Claude Code credentials this
# agent would use, so a reader of agent.json gains no privilege it did not have.
Write-Step "Restricting permissions on $InstallDir"
$me = "$env:USERDOMAIN\$env:USERNAME"
& icacls $InstallDir /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "${me}:(OI)(CI)M" | Out-Null

# -- 3. enroll the device ----------------------------------------------------
$alreadyEnrolled = $false
if (Test-Path $configPath) {
    try {
        $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
        $alreadyEnrolled = [bool]($cfg.device_id -and $cfg.secret)
    } catch { $alreadyEnrolled = $false }
}

if ($alreadyEnrolled -and -not $Force) {
    Write-Step "Device already enrolled ($($cfg.device_id)) - skipping enrollment"
} else {
    if (-not $Code) {
        throw "An enrollment code is required. Generate one in Owner OS (POST /api/v1/windows/enroll-code) and pass -Code OOS-XXXXX-XXXXX-XXXXX."
    }
    Write-Step "Enrolling this device with Owner OS"
    & $python.Source $agentPath --config $configPath enroll --server $Server --code $Code --name $DeviceName
    if ($LASTEXITCODE -ne 0) { throw "Enrollment failed. Codes are single-use and expire - generate a fresh one." }
}

# -- 4. enroll the workspace -------------------------------------------------
if ($WorkspacePath) {
    if (-not (Test-Path $WorkspacePath)) { throw "Workspace path does not exist: $WorkspacePath" }
    if (-not $WorkspaceId) {
        # Derive a stable, lowercase id from the folder name.
        $WorkspaceId = (Split-Path $WorkspacePath -Leaf).ToLower() -replace '[^a-z0-9_-]', '-'
        $WorkspaceId = ($WorkspaceId -replace '-+', '-').Trim('-')
        if ($WorkspaceId.Length -gt 64) { $WorkspaceId = $WorkspaceId.Substring(0, 64) }
    }
    Write-Step "Enrolling workspace '$WorkspaceId' -> $WorkspacePath"
    & $python.Source $agentPath --config $configPath add-workspace --id $WorkspaceId --path $WorkspacePath
    if ($LASTEXITCODE -ne 0) { throw "Workspace enrollment failed." }
} else {
    Write-Warn "No -WorkspacePath given. Add one later with:"
    Write-Warn "  python `"$agentPath`" --config `"$configPath`" add-workspace --id myproject --path C:\path\to\project"
}

# -- 5. autostart ------------------------------------------------------------
if ($NoAutostart) {
    Write-Step "Skipping autostart (-NoAutostart). Run it manually with:"
    Write-Host "    python `"$agentPath`" --config `"$configPath`" run"
} else {
    Write-Step "Registering the 'OwnerOSAgent' scheduled task (at logon, auto-restart)"
    $taskName = "OwnerOSAgent"
    $action = New-ScheduledTaskAction -Execute $python.Source `
        -Argument "`"$agentPath`" --config `"$configPath`" run" -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    # Runs as the logged-in owner, NOT as SYSTEM: Claude Code must see the same
    # user profile, PATH and credentials the owner uses interactively, and a
    # remote-control agent should hold the fewest privileges that still work.
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "    task registered and started"
}

Write-Step "Done"
Write-Host ""
Write-Host "  config    : $configPath"
Write-Host "  status    : python `"$agentPath`" --config `"$configPath`" status"
Write-Host "  logs      : Get-ScheduledTask OwnerOSAgent | Get-ScheduledTaskInfo"
Write-Host "  rotate key: python `"$agentPath`" --config `"$configPath`" rotate"
Write-Host ""
Write-Host "  This machine now makes outbound connections to $Server only." -ForegroundColor Green
Write-Host "  Owner OS can reach ONLY the workspaces enrolled above, and only" -ForegroundColor Green
Write-Host "  through start/read/send/stop - there is no remote shell." -ForegroundColor Green
