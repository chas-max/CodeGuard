# CodeGuard 评测基线一键入口（PowerShell）
#
#   .\scripts\run_eval_baseline.ps1                 # 全量离线基线
#   .\scripts\run_eval_baseline.ps1 -Smoke 30       # 30 条冒烟
#   .\scripts\run_eval_baseline.ps1 -Live -Repeat 3 # 真实 LLM + Docker
#
# 离线模式零 LLM、零网络；-Live 会真实计费，先确认 BUDGET_MAX_CNY。
param(
    [int]$Smoke = 0,
    [switch]$Live,
    [int]$Repeat = 1,
    [int]$Parallel = 1,
    [double]$Budget = 10.0,
    [string]$Out = "reports/eval_baseline"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> 校验用例生成器" -ForegroundColor Cyan
python -m eval.build_cases --check

$runArgs = @("--out", $Out, "--repeat", $Repeat, "--parallel", $Parallel, "--budget-cny", $Budget)
if ($Smoke -gt 0) { $runArgs += @("--limit", $Smoke) }
if ($Live) { $runArgs += "--live" }

Write-Host "==> 跑批：python -m eval.run_matrix $($runArgs -join ' ')" -ForegroundColor Cyan
python -m eval.run_matrix @runArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "跑批存在失败用例（预期用于优化排序，不是脚本错误）" -ForegroundColor Yellow
}

Write-Host "==> 评分卡" -ForegroundColor Cyan
python -m eval.scorecard --input "$Out/results.jsonl" --out $Out

Write-Host "==> 优化排序" -ForegroundColor Cyan
python -m eval.report_optimization --input "$Out/scorecard.json" --out $Out

Write-Host "完成：$Out/scorecard.md / $Out/optimization.md" -ForegroundColor Green
