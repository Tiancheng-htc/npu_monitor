# NPU Monitor

一个在 Windows 上运行的小工具，通过 SSH 批量连接服务器，实时展示各机器上 Ascend NPU 的空闲/占用情况。

适合从一台 Windows 开发机同时盯多台训练/推理机，直观看到"哪些卡现在没人用"。

## 功能

- 读取你指定的 `~/.ssh/config`，自动枚举其中的 Host 条目
- 并发 SSH 到每台机器执行 `npu-smi info`，5s 一轮刷新（可调 2–120s）
- 每台机一个卡片，每颗 chip 一个色块：
  - 🟢 **绿** — 无进程运行，空闲
  - 🔴 **红** — 有进程占用
  - 🟣 **紫** — 已被本工具 hold 住
  - 🟡 **黄** — 正在查询
  - ⚫ **灰** — SSH 失败 / 超时 / 无 NPU
- 鼠标悬停色块看详细：HBM、AICore%、温度、功耗、进程列表（pid/name/mem）
- 支持"只显示有空闲芯片的机器"快速筛选
- 卡片标题右侧显示 `X/16 idle` 当前机器空闲 chip 数
- **Auto-hold（抢卡模式）**：当某台 16 卡机器全部空闲时，自动 SSH 过去启动 Python 把每颗卡的 HBM 占满，避免被其他人抢走。详见下文。

## 判定"空闲"的规则

**无运行进程即空闲**，不看 AICore% 和显存占用。  
判断依据是 `npu-smi info` 输出尾部那张 `NPU / Chip / Process id` 表里是否出现对应的 (NPU, Chip) 键。

## 环境要求

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Windows | 10 / 11 | 其它 Windows 版本也能跑，但需自备 OpenSSH |
| Python | 3.10+ | 建议直接从 python.org 装并勾选 "Add to PATH" |
| OpenSSH Client | 自带 | 若 `ssh.exe` 不在 PATH：设置 → 应用 → 可选功能 → 添加 "OpenSSH 客户端" |
| PySide6 | 6.5+ | 由 `run.bat` 自动 pip 安装 |

服务器端要求：
- `npu-smi info` 可执行（Ascend 驱动已装好）
- 支持 **免密 SSH 登录**（公钥已 deploy，且私钥没有 passphrase）
  - 工具用 `BatchMode=yes` 禁止任何密码/passphrase 交互提示；有 passphrase 会直接失败

## 快速开始

1. 把整个 `npu_monitor/` 目录拷到 Windows（例如 `C:\tools\npu_monitor\`）
2. 双击 **`run.bat`**
   - 首次会自动创建 `venv/`、安装 PySide6（约 30s）
   - 之后每次秒启
3. 程序启动后：
   - **SSH config** 默认填 `%USERPROFILE%\.ssh\config`，你可以直接手填或 Browse 选文件
   - **Interval** 默认 5 秒一轮
   - 点 **▶ Start** 开始轮询

> 首次连某台机器时 SSH 会弹 host key 确认？不会，工具带了 `StrictHostKeyChecking=accept-new`，自动接受并写入 known_hosts。

## SSH config 示例

你之前贴的这种格式就可以直接用（一行一个字段或每行缩进都行，OpenSSH 两种都支持）：

```
Host 7.150.12.101
    HostName 7.150.12.101
    User root
    IdentityFile "C:\Users\h50053362\.ssh\A3"

Host 7.150.12.208
    HostName 7.150.12.208
    User root
    IdentityFile "C:\Users\h50053362\.ssh\A3"

Host 910c_yinfei
    HostName 7.150.14.2
    User root
    IdentityFile "C:\Users\h50053362\.ssh\A3"
```

工具会：
- 用 `Host` 那行的别名作为卡片标题（比如 `910c_yinfei` 会显示别名而不是 IP）
- 跳过重复的 Host 条目（同一 alias 只保留一条）
- 跳过通配条目（例如 `Host *`）

## Auto-hold（抢卡模式）

### 工作原理

1. GUI 勾选 **Auto-hold when all 16 chips idle** 时，每轮 `npu-smi info` 解析完都会检查：
   - chip 总数 ≥ 16（只针对完整的 16 卡机）
   - 所有 chip 都无进程运行
   - 进程列表里没有 `NPU_HOLD_*` 字样（说明不是自己已经占上了）
   - 本机不在 in-flight / failed 名单里
2. 条件全满足 → 把本地 `hold_npu.py` 通过 SSH stdin 推到远端 `/tmp/npu_monitor_hold.py`，`nohup` 启动
3. `hold_npu.py` 在目标机读取 `npu-smi info` 拿到每颗卡的 HBM 总量 → 为每颗 chip `fork` 一个子进程 → `torch.empty` 分配 ~90% HBM（可在 GUI 调整 10–99%）→ 阻塞
4. 每个子进程通过 `prctl(PR_SET_NAME)` 把 comm 改为 `NPU_HOLD_phyX`，父进程改为 `NPU_HOLD_run`
5. 下一轮 `npu-smi info` 看到 `NPU_HOLD_*` 进程 → 卡片显示 🔒 Held 徽章和 Release 按钮
6. 点 **Release**（或顶部 **Release all**）→ SSH 过去 `pkill -9 -f NPU_HOLD` 然后清理文件

### 开关与选项

| 控件 | 作用 |
| --- | --- |
| `Auto-hold when all 16 chips idle` | 总开关，默认关闭；首次勾选会弹确认框 |
| `Hold %` | 每卡占用的 HBM 百分比（10–99%，默认 90%）。脚本内部还会强制保留至少 2 GB 余量 |
| `Release all` | 一键释放所有已占用的主机 |
| 卡片上的 `Release` | 单机释放 |
| 卡片上的 `Retry hold` | 占卡失败后用于重试（失败原因显示在徽章上） |

### 远端依赖

占卡脚本依赖：
- `python3`
- `torch` + `torch_npu`（训练/推理环境一般都装了）
- `npu-smi` 可用（用来探测各卡 HBM 总量）

如果远端缺 `torch_npu`，脚本会立即退出，GUI 60s 后把该机标记为 `⚠ Hold failed`，直到你手动点 Retry。

### 状态机

```
         +--> dispatch ok, seen NPU_HOLD proc --+
         |                                      v
idle ----+--> dispatch ok, no proc yet -> holding
         |                                      |
         |                                      +--(>60s still no proc)--> failed
         +--> ssh error ------------------------+
                                                      (Retry hold button resets to idle)
```

### 冷/热状态

- **GUI 重启**：hold 状态靠远端 `npu-smi` 看到 `NPU_HOLD_*` 进程推断，**不依赖本地文件**。重启 GUI 后下一轮查询就能识别哪些机器还在 hold
- **目标机重启**：hold 进程会没掉，几秒后 GUI 看到 chips 又全空闲，若 Auto-hold 仍开着会再次触发占卡

### 手动占卡 / 清理（不走 GUI）

```bash
# 目标机上手动启动占卡
python3 hold_npu.py --percent 90

# 目标机上手动释放
pkill -9 -f NPU_HOLD
rm -f /tmp/npu_monitor_hold.py /tmp/npu_monitor_hold.log
```

### 注意事项 / 风险

- **合规**：此功能会在共享资源上长期占用显存，请只在你**有使用权的机器**上启用，避免影响同事
- **脚本用 `nohup`**：即使你关掉 GUI，hold 进程仍然活着。要停必须走 Release 按钮或手动 pkill
- **日志**：远端每次占卡的日志在 `/tmp/npu_monitor_hold.log`，遇到 `Hold failed` 去那里看详情

## 文件结构

```
npu_monitor/
├── main.py              # PySide6 GUI 主程序（卡片、轮询调度、hold 状态机）
├── parser.py            # npu-smi info 文本解析器（输出 → ChipStatus 列表）
├── ssh_client.py        # SSH config 解析 + ssh.exe 子进程封装（含 stdin 管道）
├── hold_npu.py          # 远端占卡脚本（fork 每卡一个，torch.empty 分配 HBM）
├── requirements.txt     # PySide6（GUI 侧）
├── run.bat              # Windows 一键启动脚本
└── README.md            # 本文件
```

GUI 侧只依赖 PySide6；没有用 paramiko — 直接调 Windows 自带的 `ssh.exe`，IdentityFile / ProxyJump / HostName 等所有 OpenSSH 原生语义都自动生效。
目标机侧占卡脚本需要 `torch + torch_npu`。

## 常见问题

**Q: 某台机显示 "SSH binary not found"**  
A: Windows 没装 OpenSSH 客户端。设置 → 应用 → 可选功能 → 添加功能 → 勾选 "OpenSSH 客户端"。装完重启 `run.bat` 即可。

**Q: 某台机一直 Offline + "Permission denied"**  
A: 密钥问题。手动开一个 PowerShell 跑 `ssh <alias>` 能通才行。可能原因：
- 私钥路径错了（SSH config 里 `IdentityFile` 指向不存在的文件）
- 私钥有 passphrase — 工具用了 `BatchMode=yes`，不能输入 passphrase，请用 `ssh-keygen -p -f <key>` 去掉密码，或改用 ssh-agent
- 目标机 `~/.ssh/authorized_keys` 没部署你的公钥

**Q: 某台机 "SSH timeout after 12s"**  
A: 网络不通或机器挂了。工具单次查询上限 12s，连接阶段 5s。下一轮 5s 刷新时会自动重试。

**Q: 某台机 "npu-smi returned no chips"**  
A: SSH 连上了但 `npu-smi` 输出不含 NPU 数据。通常是：
- 目标机不是 Ascend 环境（误加入了 config）
- `npu-smi` 不在默认 PATH（登录 shell 与非交互 shell 差异），可在 main.py 里把 `"npu-smi info"` 改成 `"/usr/local/Ascend/driver/tools/npu-smi info"` 之类的绝对路径

**Q: 想改默认 SSH config 路径**  
A: 编辑 `main.py` 里的 `default_config_path()` 方法。

**Q: 并发上限**  
A: 同时最多 16 个 SSH 进程，在 `main.py` 的 `setMaxThreadCount(16)` 处调整。

**Q: 卡片太多，屏幕放不下**  
A: 勾选 "Only show hosts with idle chips" 只看有卡可用的机器；或拖大窗口（卡片自动纵向排）。

## 用到的 SSH 参数

```
ssh -F <config> <host> "npu-smi info"
    -o BatchMode=yes                # 禁止一切交互提示
    -o ConnectTimeout=5              # TCP 连接 5s 超时
    -o ServerAliveInterval=3         # 3s 一次心跳
    -o ServerAliveCountMax=2         # 连续 2 次无响应就断开
    -o StrictHostKeyChecking=accept-new   # 首次连接自动信任
    -o LogLevel=ERROR                # 过滤 SSH 自身的 warning
```

子进程整体 timeout 设为 12s。

## 打包成单文件 exe（可选）

如果不想让使用者装 Python，可以用 PyInstaller 打包：

```bat
call venv\Scripts\activate.bat
pip install pyinstaller
pyinstaller --onefile --windowed --name NpuMonitor main.py
```

产物在 `dist\NpuMonitor.exe`，可直接拷到任何 Windows 机器双击运行（仍需目标机有 OpenSSH Client）。

## 扩展方向

当前实现故意保持简单。如果需要更多能力：

- **换"空闲"判定规则**：编辑 `parser.py::ChipStatus.is_idle`
- **改最少卡数阈值**（比如 8 卡机也抢）：改 `main.py::MIN_CHIPS_FOR_AUTO_HOLD`
- **加自定义命令**（比如同时看 CPU/内存）：`main.py::HostRunnable.run` 里再发一次 `run_ssh`
- **导出报表**：`on_host_finished` 里把结果写 CSV
- **告警**：比如有机器从 Busy 变 Idle 时弹 Windows 通知 — 在 `on_host_finished` 里对比前后状态调 `QSystemTrayIcon.showMessage`
- **占卡方式换成 ACL 直接 malloc**（不依赖 torch_npu）：改 `hold_npu.py::holder_proc`，改用 `ctypes.CDLL('libascendcl.so')` 调 `aclrtSetDevice` + `aclrtMalloc`
