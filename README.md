# ustc_auto_elective
ustc_auto_elective，用你的agent配置选课吧

非官方中国科学技术大学选课助手：Python 命令行、官网登录会话复用、批量人数监控、
显式授权的选课与退旧选新。结构参考 PKUAutoElective，**不兼容 PKU 的认证协议**。

默认只读；不附带任何真实账户、课程计划或登录凭据。`config.sample.ini` 中的课堂代码均为虚构示例。

> 使用前请确认学校允许自动化访问，并遵守选课规则。工具不保证选上课程，不绕过学分、时间冲突、
> 选课资格、验证码或访问限制。自动退课有丢失原课程名额的风险，务必先读“退旧选新”。

## 功能与边界

- 一个 Python 进程同时监控多个目标，合并为一批人数查询，不在页面上逐个搜索。
- 按配置顺序尝试目标，支持前置课程、要求已退课程、互斥选项与候选班优先级。
- 每轮输出时间、轮次、人数、已选学分与等待原因；另有本地日志及状态心跳。
- 请求失败自动退避；429 尊重 `Retry-After`；恢复后不补发积压请求。
- 默认 `monitor` 只读；`auto` 才选课，退课需要额外的规则和风险确认字段。
- 每次写操作先记录事务，核对官网结果与最新课表；结果不明时停止后续写操作。
- 锁屏或熄屏不依赖页面继续工作；可在 Windows 临时申请防自动睡眠。

当前支持已适配的教务选课接口和单一教学班模式。复杂排课、多教学班、未知课表格式会阻止自动操作。
本地冲突判断不是学校的最终资格判定，不包含完整考试冲突、培养方案或课程类别审核。
仅提供本地控制台与日志提醒，未实现邮件、短信或推送服务。

## 环境与安装

建议 Windows 10/11、Python 3.12、已安装的 Microsoft Edge。无需浏览器扩展，也不依赖 Codex。
依赖锁定于经过本地测试的 Playwright 版本；系统 Edge 及学校接口升级仍可能影响兼容性。
离线 CI 同时覆盖 Windows/Linux，不表示已经验证 Linux 上的实际教务登录；防睡眠仅支持 Windows。

在项目目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.sample.ini config.ini
```

下文简写 `python`，请替换成 `.\.venv\Scripts\python.exe`，或先激活该环境。
程序通过 `channel='msedge'` 调用已安装 Edge，不需要执行 Playwright 的浏览器下载命令。

## 第一次使用

### 账号密码自动登录

复制示例为 `config.ini` 后，在本地填写以下字段，并填写目标课程、学分上限：

```ini
[auth]
auto_login = true
username = 你的学号
password = 你的统一身份认证密码

[page]
course_select_url = 粘贴本人当前批次的完整选课页地址
```

中文占位符必须替换。首次无人值守启动必须提供完整选课页地址，不能用教务主页代替；
程序目前不自动发现学生编号和选课批次。有保存的会话时可从会话读取此地址。
先执行 `python main.py check-config`，再执行 `python main.py monitor --dry-run --cycles 3`。
没有会话或会话过期时，先尝试 SSO，再使用配置中的凭据登录；验证成功后恢复查询。
确认只读结果正确后，将 `[helper] mode = auto`，运行 `python -u main.py monitor` 才会自动选课。

这是 Python 启动后台 Edge 完成官方认证，不依赖扩展、Codex 或你手动打开的窗口。
需要安装 Edge；验证码、二次认证、密码过期等情况仍需人工处理。
自动登录已做合成数据测试，尚未用真实账号密码验证完整认证流程。

### 不保存密码：手动登录

保持 `auto_login = false`（或不写 `[auth]`），按以下步骤操作：

1. 编辑 `config.ini`：填自己的完整课堂代码和学分上限，保留 `mode = monitor`。
2. 离线检查配置：

   ```powershell
   python main.py check-config
   ```

3. 登录：

   ```powershell
   python main.py login
   ```

   程序打开一个独立 Edge 窗口。你在官网完成认证、必要的验证码或多因素验证，
   并进入自己当前批次的具体选课页面。成功核验已选课程接口后，程序保存会话并关闭此窗口。
   只打开教务主页不算完成。首次可让 `[page] course_select_url` 留空，由此流程识别。

4. 验证并进行三轮只读检查：

   ```powershell
   python main.py check-session
   python main.py monitor --dry-run --cycles 3
   ```

5. 确认目标、人数、前置条件、冲突和学分都正确，再持续监控：

   ```powershell
   python -u main.py monitor
   ```

`--dry-run` 强制禁止选退课，即使配置写了 `auto`。它仍会读取官网、更新本地日志和心跳。
`check-config` 只核验格式和部分逻辑，不访问网络，也不能证明课堂存在或符合培养要求。
修改配置后应先安全停止再重启，不支持热加载 INI。

## 配置说明

完整可复制示例见 [config.sample.ini](config.sample.ini)。不需要的示例目标可删除或设为 `enabled = false`。
一个最小目标配置如下，代码须自行替换：

```ini
[page]
course_select_url =

[helper]
mode = monitor
poll_interval_seconds = 30
max_credits = 20
max_run_minutes = 0
prevent_idle_sleep = false
unscheduled_selected_codes =

[course:my_target]
enabled = true
code = DEMO1001P.01
```

| 配置项 | 含义 |
| --- | --- |
| `mode` | `monitor` 只读；`auto` 允许选课及已显式启用的退课 |
| `poll_interval_seconds` | 正常情况下人数查询的最小起始间隔，默认 30 秒，最低 5 秒 |
| `max_credits` | 自己当前批次的学分上限，必须填写正数；示例 20 不是学校统一规定 |
| `max_run_minutes` | 0 持续运行；正数限制运行时长 |
| `prevent_idle_sleep` | Windows 临时防自动空闲睡眠；默认 false |
| `course_select_url` | 可留空从登录流程识别；若填写须为本人当前批次的官方 HTTPS 完整选课 URL |
| `unscheduled_selected_codes` | 默认空。人工确认某已选课程确实无排课后，才可逐项列入豁免 |

每个 `[course:名称]` 支持：

- `code`：官网完整课堂代码，含 `.01` 等班号，不是课程名称或内部数字 ID。
- `enabled`：是否列入目标，默认 true。
- `group`：互斥组选项，英文/数字/下划线/连字符；同组最多选一项。
- `requires_selected`：选课前必须全部在已选课表中的代码，逗号分隔。
- `requires_absent`：选课前必须全部不在已选课表中的代码，逗号分隔。

目标按 INI 中出现的顺序尝试。“优先”指第一项不可选时允许尝试下一项，
不是无限等待首选；选上备选后，不会自动退掉备选去换首选。
同一课程基础代码的不同班次也会互斥，包括未列入目标配置的已选班次。
前置条件仅用于加课，不会帮你自动选或退其列出的课程；自动退课必须另配规则。

时间或周次无法解析时默认停写。豁免仅对“上课时间和周次两项都空”的指定已选课程生效，
不会豁免目标课程、部分缺失的数据或已有排课。启用豁免意味着你接受程序无法判断该课冲突的风险。
周次采用保守的起止区间，可能把不连续周次误判为冲突，不会推断跳过未知时段。

## 退旧选新：必须理解的风险

无冲突时正常选课，**不会为腾学分主动退课**。有冲突时，只有配置明确授权的唯一冲突课才能退；
多个冲突、额外未授权冲突或替换后超过学分上限，都不会执行替换。

```ini
[swap:replace_one]
enabled = false
target_course_code = DEMO1001P.01
conflict_drop_course_code = OLD1001P.01
acknowledgement = DROP_THEN_ADD

[after-select:remove_backup]
enabled = false
trigger_course_code = DEMO1001P.01
drop_course_code = BACK1001P.01
acknowledgement = DROP_AFTER_SUCCESS
```

只有同时将全局模式改成 `auto`、相应规则 `enabled = true`、确认字符串填写正确，
对应退课规则才会生效。目标必须列入已启用的 `[course:*]`。
退课对象不能也是监控目标或任何目标的 `requires_selected`，避免自相矛盾。

流程：

```text
人数显示有余量 + 前置条件通过
  ├─ 无冲突：检查学分 → 选目标
  └─ 唯一冲突且明确授权：检查替换后学分 → 退旧课 → 官网核验 → 重新检查 → 选目标
      ↓
选课响应与官网已选课表一致
      ↓
执行显式配置的成功后退课 → 官网核验 → 下一轮才考虑后续目标
```

这不是原子交换。退旧期间名额可能被别人占用；仅在目标加课明确失败、课表确认未选上且恢复条件可验证时，
程序会尝试恢复原课一次。原课名额也可能已失去，因此回滚不保证成功。
只要请求结果未知或结果与课表不一致，就保留事务记录并停写，不重放请求，也不猜测回滚。

`after-select` 也适用于启动时目标已经选上的情况，可能立即退掉配置的旧课；
它不是“必须由本轮程序刚刚选上”的限制。请特别核对。

## 观察运行、停止与锁屏

控制台持续打印轮次，例如以下虚构输出：

```text
INFO 第12轮 | 已选学分=16.0/20 | DEMO1001P.01 人数 40/40（已满）
WARNING 第13轮 | RATE_LIMIT | 60s 后重试；本轮未继续自动操作。
```

另开终端：

```powershell
python main.py status
Get-Content .local\monitor\monitor.log -Tail 20 -Wait
python main.py stop
```

`status` 是最后心跳快照，不保证进程仍存活；请结合心跳时间和日志轮次判断。
日志自动轮换，每个约 5 MB，保留 3 个备份。终端运行不会因锁屏暂停，但关闭终端可能结束进程。
需要独立后台运行时，可使用 PowerShell（仍须预先完成登录和只读验证）：

```powershell
New-Item -ItemType Directory -Force .local\monitor | Out-Null
$monitorPython = (Resolve-Path .venv\Scripts\python.exe).Path
$monitorEntry = (Resolve-Path main.py).Path
Start-Process -FilePath $monitorPython -ArgumentList '-u', ('"' + $monitorEntry + '"'), 'monitor' -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -RedirectStandardOutput .local\monitor\console.log -RedirectStandardError .local\monitor\stderr.log
```

先确认没有旧实例再运行。程序按项目目录加锁，不支持多个目录、浏览器扩展或其他脚本同时自动操作同一账户。
需要多个目标时写入同一份配置，不要“双开”多个写进程。

Windows 的 `prevent_idle_sleep = true` 只在运行期间请求阻止自动空闲睡眠，退出时释放，不修改电源方案。
允许锁屏、熄屏；可用 `powercfg /requests` 检查。它不能保证阻止主动睡眠、合盖、关机、断网或系统策略。
真正睡眠期间不可能发网络请求，唤醒后会恢复查询而不补发；请自行确保电源与网络可用。
本项目未设置开机自启或系统计划任务。

## 登录会话

会话仅存于本机 `.local/auth/session.json`，不保存密码或表单数据；Cookie 本身仍是敏感凭据。
启用 `[auth] auto_login = true` 时，密码明文保存在你编辑的 `config.ini` 中。
不要上传、分享该文件，也不要在 `config.sample.ini` 填真实凭据。
密码中的 `%` 无需转义；不要额外加引号（引号会算入密码）。

有效会话优先复用；失效时先尝试 SSO，再按配置提交账号密码。
每次认证恢复期间最多调用一次自动账号密码登录；只有恢复正常查询后，才允许下次失效时再次尝试。
失败后保持等待，不反复提交密码。请先核对密码，必要时暂设 `auto_login = false`，
在另一终端运行 `python main.py login` 完成人工验证；保存新会话后监控会重新加载。
配置不热加载；修改后安全停止并重启。不要在错误密码状态下设置无限自动重启。
`login` 命令也遵循 `[auth]`：启用时自动提交，关闭时手动登录。

可选 `login --prompt-credentials` 在交互终端输入一次账号密码，由官方页面提交，不写入配置，
不实现验证码识别或密码自动重试。使用此选项时请关闭配置中的 `auto_login`。
学校认证流程改变时，需要重新适配；不承诺永久免登录。

## 故障处理

| 提示/现象 | 处理 |
| --- | --- |
| 课堂代码未唯一匹配 | 核对批次、完整班号和是否开放；程序不把“未显示”等同于“未开课”，其他已找到目标继续监控 |
| `AUTH` / `waiting_auth` | 会话可能失效或权限被拒绝；用官方页面核对并重新 login，不切换 IP 绕过 |
| `RATE_LIMIT` | 按服务器要求等待；不要并开实例或降低间隔。很长的 Retry-After 会很长时间不发请求，可安全停止后联系学校 |
| `SCHEMA` | 官网响应格式与适配不符；停止 auto，提交脱敏复现，不猜字段继续执行 |
| 时间信息无法确认 | 先在官网人工确认；不要随意添加豁免来消除提示 |
| 无法获取进程锁 | 当前目录可能已有监控，先看 status 和日志，不要复制目录绕过锁 |
| 检测到未完成事务 | 依照下列步骤人工恢复，不能盲目删除记录 |

事务恢复：先运行 `stop` 并确认进程退出；到官网核对目标与旧课的真实状态、确认没有仍在处理的请求。
必要时由你在官网手动处理，并调整配置，避免再次退课。**只有确认状态稳定且不再有未决请求后**，
将 `.local/monitor/transaction.json` 移到本机私有备份目录，再运行只读三轮核对。
确认无误后才重新启用自动操作。备份也不能公开。

## 请求频率与动态 IP

不需要动态 IP，也不提供“躲避监控”模式。IP 变化可能破坏会话，不能保证绕过风控；
不要轮换代理、伪装账户或绕过验证码/封禁。
默认每 30 秒合并查询人数，最低 5 秒不是学校批准的安全阈值：应以学校要求为准。
每轮还读取已选课程，课程元数据约每 60 秒刷新；交易期间有额外结果核验，
因此“人数间隔”不等于所有 HTTP 请求都相隔该时长。
慢请求、事务、限流、断网会延长间隔，不承诺严格实时。

PKUAutoElective 的历史说明提醒过短刷新会增加服务器负担、可能触发 IP 限流；
应借鉴请求节制与诊断日志，不能把旧系统的建议当作当前科大规则。
参考：[PKUAutoElective](https://github.com/zhongxinghong/PKUAutoElective)。

其他参考只用于结构和流程理解，不作为当前接口可用性的证明：

- [ustc_grab_classes](https://github.com/sakura-umi/ustc_grab_classes)：历史项目的监控/操作模式与可调查询间隔。
- [class-arrange](https://github.com/RaymondzyLei/class-arrange)：可见 SSO 登录后复用本地浏览器会话的流程。
- [Playwright 认证文档](https://playwright.dev/python/docs/auth)：会话状态保存的使用与敏感性。
- [Windows 电源请求文档](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate)：防自动睡眠的边界。

本项目未复制其他项目的验证码识别、代理池、多账号池或 PKU 专用登录实现；仅参考项目组织方式。

## 目录与测试

```text
USTCAutoElective/
  main.py                   命令行入口
  config.sample.ini         虚构示例；自行复制为 config.ini
  autoelective/
    cli.py                  子命令
    session.py              官网登录和本地会话
    client.py               严格校验的接口访问
    config.py               配置/授权校验
    engine.py               冲突、前置条件及选退课事务
    monitor.py              轮询、日志、进程锁、防睡眠
  tests/                    纯合成数据离线回归测试
  tools/build_release.py    白名单打包及基础隐私检查
  .github/workflows/        Windows/Linux 离线测试
  .local/                   运行时生成，绝不发布
```

```powershell
python -m unittest discover -s tests -v
python -m compileall -q autoelective main.py
python main.py check-config --config config.sample.ini
python tools/build_release.py
```

测试不访问教务、不需要账号、不执行真实选退课。公开重构版验证重点是离线协议模拟与事务保护，
不代表所有学院、学期、账号或当前网站更新均已实测；首次使用务必走只读流程。

## 上传 GitHub 前

发布脚本只打包白名单源文件到 `dist/USTCAutoElective.zip`，不会包含个人配置、会话、日志、HAR 或缓存。
基础模式扫描不是隐私审计保证，仍请人工检查要上传的文件和 Git 暂存区。
不要直接上传自己正在运行的整个项目文件夹，不要上传私人项目历史。

在新建的空 GitHub 仓库中发布这份干净源码，或解压发布 ZIP 后初始化本地仓库。
`.gitignore` 已排除运行数据，但不能补救已提交过的秘密。详细处理见 [SECURITY.md](SECURITY.md)。

采用 [MIT License](LICENSE)。这是社区工具，与学校及 PKUAutoElective 维护者无隶属关系。
