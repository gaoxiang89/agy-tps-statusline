# agy-tps-statusline

Cross-platform (Linux, macOS, Windows), zero-dependency status line for **Antigravity CLI (`agy`)**.

显示端到端模型输出速率（`tok/s`）、实时等待耗时（`⏳`）、输出 token 总量、上下文窗口占用、Prompt 缓存命中率、配额余量以及当前模型，并内置兼容 `agy-swap` 的零开销配额缓存同步。

---

## 核心优化 (Why This Exists)

1. **剥离本地工具耗时（Accurate `tok/s`）**
   `agy` 在执行本地工具（`run_command`、编译、等待审批等）期间保持 `agent_state == "working"`，且 `total_output_tokens` 仅在单步模型响应结束时跳变一次。
   本工具仅在检测到 token 跳变时读取对应会话 `transcript.jsonl` 末尾（`64 KB`），提取最新模型请求发出的时间戳 `created_at`（同时兼容 `agy` 1.5.8 `transcript_path` 指向 `.gemini/antigravity/...` 的路径偏差），从而从分母中剥离工具耗时。
2. **消除假实时速率误导（Live `⏳` Indicator）**
   当模型正在思考/执行或排队超过 `2s` 尚未产生新跳变时，将上一步速率置灰并展示当前已等待时长（如 `⚡ 64.5 tok/s ⏳ 12s`）。
3. **零多余进程与最小写盘（Low Overhead at ~3Hz）**
   - 纯标准库单进程，不再通过 `sh -> agyswap -> sh -> agy-tps-statusline` 四层派生；
   - 默认不写 `last_statusline_input.json`（设置 `AGY_TPS_DEBUG=1` 可开启调试转储）；
   - 仅在状态真正变化时原子写入 `cache/tps/<session_id>.json`；
   - 若本机存在 `~/.agy-swap`，通过 `os.stat` 节流（10 秒）自动同步 Schema 2 配额缓存到 `~/.agy-swap/cache/quota.json`。
4. **终端宽度自适应（Responsive Layout）**
   根据 `agy` 传入的 `terminal_width` 自动裁剪上下文、缓存命中率与配额段，窄终端下也不会溢出折行。
5. **Prompt 缓存命中率（`cache 87% (last 93%)`）**
   从 `transcript.jsonl` 的每步 `input_tokens`（未命中）与 `cache_read_tokens`（命中）累加，命中率 = `cache_read / (input + cache_read)`。前者为会话累计（按 token 加权），`last` 为最近一次模型请求。仅在 token 跳变或 working/idle 切换时增量读取末尾 64 KB（首次或窗口不足时全量扫描一次），结果持久化在会话状态文件中。颜色：≥70% 绿、≥30% 黄、其余灰。

---

## 安装与配置 (Linux / macOS / Windows)

> **前置条件**：已安装 Python 3.9+（使用 `uv` 或 `pipx` 时会自动管理 Python 运行时）。

### 方式一：使用 `uv`（推荐，全平台通用）

**Linux / macOS:**

```bash
uv tool install --from "git+https://github.com/gaoxiang89/agy-tps-statusline" agy-tps-statusline
agy-tps-statusline install
```

**Windows (PowerShell):**

```powershell
uv tool install --from "git+https://github.com/gaoxiang89/agy-tps-statusline" agy-tps-statusline
uv tool update-shell   # 如果提示找不到 agy-tps-statusline，运行后重开终端
agy-tps-statusline install
```

> **如果以前手动安装过旧版脚本（`~/.gemini/bin/agy-tps-statusline.*` 或 `~/.gemini/antigravity-cli/bin/agy-tps-statusline.*`）**：
> 旧脚本不认识 `install` 子命令且可能在 `PATH` 中优先级高于 `uv` 的 `~/.local/bin`，会导致运行 `agy-tps-statusline install` 时卡在 `sys.stdin.read()` 或显示旧版简陋状态栏。可先删掉旧脚本再运行，或直接用 `uv` 安装的绝对路径执行（会自动备份清理两个目录下的旧文件）：
>
> ```powershell
> Remove-Item "$env:USERPROFILE\.gemini\bin\agy-tps-statusline*","$env:USERPROFILE\.gemini\antigravity-cli\bin\agy-tps-statusline*"
> agy-tps-statusline install
> # 或者直接运行：
> & "$env:USERPROFILE\.local\bin\agy-tps-statusline.exe" install
> ```

### 方式二：使用 `pipx`

```bash
pipx install "git+https://github.com/gaoxiang89/agy-tps-statusline"
agy-tps-statusline install
```

### 方式三：本地克隆 / 开发模式

```bash
git clone https://github.com/gaoxiang89/agy-tps-statusline
cd agy-tps-statusline
uv tool install --editable --from . agy-tps-statusline
agy-tps-statusline install
```

或者不安装工具包直接用系统 Python 运行：

```bash
python -m agy_tps_statusline install    # Windows 可用 py -m agy_tps_statusline install
```

---

## 常用命令

```bash
agy-tps-statusline install              # 写入 ~/.gemini/antigravity-cli/settings.json（自动备份原配置）
agy-tps-statusline install --stack      # 设置 stack_with_default = true（保留 agy 默认状态栏）
agy-tps-statusline install --no-stack   # 设置 stack_with_default = false
agy-tps-statusline doctor               # 检查路径、配置、状态缓存与示例渲染
agy-tps-statusline upgrade              # 从 GitHub 拉取并升级到最新版本（支持 --from 指定源）
agy-tps-statusline uninstall            # 将 statusLine.enabled 设为 false
```

如果本机之前通过 `agy-swap` 配置了 `agy-statusline --chain "agy-tps-statusline"`，`install` 会自动备份并升级该包装脚本为直接执行 `agy-tps-statusline`，无需重启正在运行的 `agy` 会话即可切换为单进程，同时继续同步 `~/.agy-swap/cache/quota.json`。

---

## 环境变量

| 变量 | 说明 |
|---|---|
| `AGY_GEMINI_DIR` | 覆盖 `~/.gemini` 根目录 |
| `AGYSWAP_DATA_DIR` | 覆盖 `~/.agy-swap` 根目录 |
| `AGY_TPS_NO_AGYSWAP` | 设为 `1` 时禁用自动同步 `agy-swap` 配额缓存 |
| `AGY_TPS_DEBUG` | 设为 `1` 时将每次收到的原始 JSON 写入 `cache/last_statusline_input.json` |
