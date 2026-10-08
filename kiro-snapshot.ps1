<#
.SYNOPSIS
  Kiro 本地版本快照工具（仅本机可见，不会被 git push 到 gitlab）。

.DESCRIPTION
  用 git 的自定义引用命名空间 refs/kiro-snapshots/ 存快照。
  git push 默认只推分支(refs/heads/*)，这个命名空间天然不会上传。

  每个快照捕获当前工作区的全部改动，包括：
    - 已修改的已跟踪文件
    - 已暂存的改动
    - 新增的未跟踪文件（.gitignore 忽略的除外）
  快照不改变你的工作区，也不动暂存区，可以随时打。

.PARAMETER Action
  snap    : 打一个新快照，自动分配递增版本号(v1, v2, ...)。第二个参数是描述。
  list    : 列出所有快照。
  show    : 查看某个快照的改动。第二个参数是版本号。
  restore : 把工作区恢复到某个快照。第二个参数是版本号。
  clean   : 删除该仓库的全部快照。

.EXAMPLE
  .\kiro-snapshot.ps1 snap "修复条数对账 before"
  .\kiro-snapshot.ps1 list
  .\kiro-snapshot.ps1 show v3
  .\kiro-snapshot.ps1 restore v3 -Repo C:\Users\enanshn\Projects\rvc-frontend
  .\kiro-snapshot.ps1 clean
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('snap', 'list', 'show', 'restore', 'clean')]
    [string]$Action,

    # 第二个位置参数：snap 时是描述，show/restore 时是版本号。
    [Parameter(Position = 1)]
    [string]$Arg,

    [string]$Message,
    [string]$Version,
    [string]$Repo = "."
)

$ErrorActionPreference = 'Stop'
$RefPrefix = 'refs/kiro-snapshots'

# 显式 -Message / -Version 优先，否则用位置参数兜底。
if ($Action -eq 'snap' -and -not $Message) { $Message = $Arg }
if (($Action -eq 'show' -or $Action -eq 'restore') -and -not $Version) { $Version = $Arg }

# 所有 git 调用都直接写 & git -C $Repo ...，不做参数包装
# （包装函数对 -d 这类 flag 参数绑定不可靠，是之前的 bug 来源）。

function Assert-Repo {
    $inside = & git -C $Repo rev-parse --is-inside-work-tree 2>$null
    if ($LASTEXITCODE -ne 0 -or $inside -ne 'true') {
        throw "目标目录不是 git 仓库: $Repo"
    }
}

function Normalize-Version {
    param([string]$V)
    if (-not $V) { throw "缺少版本号，请写在动作后面，如: restore v3" }
    if ($V -match '^v?(\d+)$') { return "v$($Matches[1])" }
    throw "版本号格式不对: $V （应为 v3 或 3）"
}

# 列出所有快照，按版本号数字排序
function Get-Snapshots {
    $lines = & git -C $Repo for-each-ref --format='%(refname)|%(subject)|%(creatordate:iso)' $RefPrefix
    $items = @()
    foreach ($line in $lines) {
        if (-not $line) { continue }
        $parts = $line -split '\|', 3
        $shortname = $parts[0] -replace "^$RefPrefix/", ''
        if ($shortname -match '^v(\d+)$') {
            $items += [pscustomobject]@{
                Num = [int]$Matches[1]; Version = $shortname; Ref = $parts[0]
                Subject = $parts[1]; Date = $parts[2]
            }
        }
    }
    return $items | Sort-Object Num
}

function Next-Version {
    $snaps = Get-Snapshots
    if ($snaps.Count -eq 0) { return 1 }
    return ($snaps | Select-Object -Last 1).Num + 1
}

# 造一个包含当前工作区全部改动(含未跟踪新文件)的 commit，返回其 oid。
# 不改变工作区，也把 index 还原回调用前的状态。
function New-SnapshotCommit {
    param([string]$Msg)
    $head = & git -C $Repo rev-parse HEAD
    $savedIndex = & git -C $Repo write-tree      # 记住当前 index
    & git -C $Repo add -A | Out-Null             # 未跟踪新文件也纳入
    $tree = & git -C $Repo write-tree
    $commit = & git -C $Repo commit-tree $tree -p $head -m $Msg
    & git -C $Repo read-tree $savedIndex | Out-Null  # 还原 index，不动工作区
    return $commit
}

Assert-Repo

switch ($Action) {

    'snap' {
        if (-not $Message) { throw "打快照必须带描述：snap `"你的描述`"" }
        $commit = New-SnapshotCommit $Message
        $ver = "v$(Next-Version)"
        & git -C $Repo update-ref "$RefPrefix/$ver" $commit
        Write-Host ""
        Write-Host "  已记录快照 [$ver]  ($Repo)" -ForegroundColor Green
        Write-Host "  描述: $Message"
        Write-Host "  回退命令: .\kiro-snapshot.ps1 restore $ver -Repo `"$Repo`""
        Write-Host ""
    }

    'list' {
        $snaps = Get-Snapshots
        if ($snaps.Count -eq 0) {
            Write-Host "（$Repo 暂无快照）" -ForegroundColor Yellow
            break
        }
        Write-Host ""
        Write-Host "  仓库: $Repo" -ForegroundColor Cyan
        Write-Host ""
        foreach ($s in $snaps) {
            "{0,-5} {1}  {2}" -f $s.Version, $s.Date, $s.Subject | Write-Host
        }
        Write-Host ""
    }

    'show' {
        $ver = Normalize-Version $Version
        $ref = "$RefPrefix/$ver"
        $exists = & git -C $Repo rev-parse --verify --quiet $ref 2>$null
        if (-not $exists) { throw "找不到快照 $ver" }
        & git -C $Repo show --stat $ref
    }

    'restore' {
        $ver = Normalize-Version $Version
        $ref = "$RefPrefix/$ver"
        $commit = & git -C $Repo rev-parse --verify --quiet $ref 2>$null
        if (-not $commit) { throw "找不到快照 $ver" }

        # 恢复前自动打一个安全快照，回退后后悔还能找回来
        $backup = New-SnapshotCommit "auto-backup before restore to $ver"
        $bver = "v$(Next-Version)"
        & git -C $Repo update-ref "$RefPrefix/$bver" $backup

        # 完全一致回退：工作区最终与快照【逐字节一致】。
        # 分三步，全程不移动 HEAD（不动你的分支指针）：
        #   1. checkout 覆盖/恢复所有已跟踪文件为快照内容
        & git -C $Repo checkout $commit -- . | Out-Null
        #   2. 把快照的树读进 index，让 index 反映"快照里有哪些文件"
        & git -C $Repo read-tree $commit | Out-Null
        #   3. git clean 删除 index 里没有、但工作区存在的文件（即快照之后新建的）。
        #      -f 强制，-d 含目录。默认【不】碰 .gitignore 忽略的文件
        #      （node_modules / data/output / data/logs 等运行产物和依赖，安全保留）。
        $cleaned = & git -C $Repo clean -fd
        #      把 index 还原成快照对应状态，让后续 git status 正确反映回退结果。
        & git -C $Repo read-tree $commit | Out-Null

        Write-Host ""
        Write-Host "  已完全回退工作区到 [$ver]  ($Repo)" -ForegroundColor Green
        Write-Host "  恢复前状态已自动存为 [$bver]（后悔可 restore $bver）"
        if ($cleaned) {
            Write-Host "  已删除快照之后新建的文件:"
            foreach ($c in $cleaned) { Write-Host "    $c" }
        }
        Write-Host "  (未触碰 .gitignore 忽略的依赖/运行产物，如 node_modules、data/)"
        Write-Host ""
    }

    'clean' {
        $snaps = Get-Snapshots
        if ($snaps.Count -eq 0) {
            Write-Host "（$Repo 无快照可清理）" -ForegroundColor Yellow
            break
        }
        $deleted = 0
        foreach ($s in $snaps) {
            & git -C $Repo update-ref -d $s.Ref
            if ($LASTEXITCODE -eq 0) { $deleted++ }
        }
        Write-Host "  已删除 $deleted 个快照  ($Repo)" -ForegroundColor Green
    }
}
