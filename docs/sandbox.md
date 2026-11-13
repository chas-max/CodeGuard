# Docker 沙箱

## 构建

```bash
docker build -f dockerfiles/sandbox/python.Dockerfile -t codeguard-sandbox-python:3.12 .
```

## 安全不变量

- `network_mode="none"`（测试阶段）
- `read_only=True` 根文件系统；仅 `/workspace` 可写
- 非 root `1000:1000`
- `cap_drop=["ALL"]`、`no-new-privileges`
- 无 Docker socket、无宿主根目录、无 SSH key、无宿主 `.env`
- `mem_limit`、`nano_cpus`、`pids_limit`
- 超时 kill + 强制 remove

## 工作区

每个 job 使用独立目录；容器只挂载该目录。宿主机只做 git 操作与 AST 解析，不执行 PR 代码。
`prepare` 阶段可选联网安装 base 依赖，默认关闭；测试阶段始终无网络。缺依赖导致失败归类为基础设施错误，不归因代码。

## Agent 工具执行边界

Agent 的 `run_tests` / `run_check` / `run_command` 与 `validate` 节点复用同一个
`SandboxBackend.run(spec)`：每次工具调用起一个一次性容器，`fix_root` 通过 bind mount
挂到 `/workspace`，文件改动持久落在宿主副本，容器用完即删。

- `network` 恒为 `none`，`prepare` 的联网能力不开放给 Agent；
- 单次超时取 `min(policy.sandbox.timeout_sec, SANDBOX_TIMEOUT_SEC,
  agents.tools.sandbox_timeout_sec, 工具循环剩余墙钟)`；
- 并发由 `ReviewNodes.sandbox_slots`（`BoundedSemaphore(SANDBOX_MAX_CONCURRENCY)`）控制，
  工具循环本身串行，不会自造并发；
- `run_command` 只接受 argv 列表并逐元素 `shlex.quote`，先过禁止列表（网络/安装/推送/提权/
  宿主 shell），再过 `agents.tools.allow_commands` + 策略测试命令白名单；
- 写工具只对 fixer 开放，只写 `data/jobs/<job_id>/fix_<n>`，且必须落在 `allowed_files`
  内、不得改测试/受保护路径；Agent 写入后由 `worktree_diff` 生成补丁，仍在干净副本上跑
  `git apply --check`。

