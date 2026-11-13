# CodeGuard 评测平台一键入口
#
#   .\scripts\run_bench.ps1                          # 离线核心样例 + 报告
#   .\scripts\run_bench.ps1 -Limit 12                # 指定样例数
#   .\scripts\run_bench.ps1 -Verify                  # 先校验物化数据
#   .\scripts\run_bench.ps1 -Live -PatchSource gold  # 外部基准（需快照 + Docker）
param(
    [string]$Dataset = "core",
    [int]$Limit = 8,
    [string]$Out = "reports/bench_demo",
    [switch]$Fixtures,
    [switch]$Verify,
    [switch]$Live,
    [ValidateSet("gold", "system")]
    [string]$PatchSource = "gold",
    [string]$SnapshotRoot = "eval/datasets/bench/snapshots"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if ($Verify) {
    Write-Host "==> 校验物化数据" -ForegroundColor Cyan
    python -m eval.bench fetch --verify
}

$runArgs = @("run", "--dataset", $Dataset, "--out", $Out, "--patch-source", $PatchSource)
if ($Limit -gt 0) { $runArgs += @("--limit", $Limit) }
if ($Fixtures -or $Dataset -eq "core") { $runArgs += "--fixtures" }
if ($Live) {
    $runArgs += @("--live", "--snapshot-root", $SnapshotRoot)
}

Write-Host "==> python -m eval.bench $($runArgs -join ' ')" -ForegroundColor Cyan
python -m eval.bench @runArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "评测执行失败（不是质量门槛失败）" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "==> 重新渲染报告" -ForegroundColor Cyan
python -m eval.bench report --input $Out --quiet
Write-Host "完成：$Out/report.md" -ForegroundColor Green
