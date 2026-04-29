param(
    [Parameter(Mandatory = $true)]
    [string]$Message
)

$ErrorActionPreference = "Stop"

function Run-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Args
    )

    & git @Args
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Args -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if (-not $branch) {
    throw "Cannot detect current git branch."
}

Run-Git -Args @("add", "-A")

$stagedDiff = git diff --cached --name-only
if (-not $stagedDiff) {
    Write-Host "No staged changes. Nothing to commit."
    exit 0
}

Run-Git -Args @("commit", "-m", $Message)
Run-Git -Args @("pull", "--rebase", "origin", $branch)
Run-Git -Args @("push", "origin", $branch)

Write-Host "Done: committed and synced branch '$branch'."
