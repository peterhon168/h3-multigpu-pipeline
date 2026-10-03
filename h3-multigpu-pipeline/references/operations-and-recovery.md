# 运行与恢复

## 当前 CLI 命令面

从本 Skill 目录运行 `python3 scripts/pipeline.py <command>`。当前入口包含：

- `plan`：离线接受 `--mode`、`--width`、`--height`、`--seconds`，可设 `--count`、`--seed`。
- `config-adapt`：用 `--host-map` 合并本地私有主机映射，可设 `--out` 与 `--dry-run`；拒绝覆盖已存在的配置文件。
- `preflight --mode MODE`：离线检查所选服务的本地配置、端口字段、SSH 配置形状与包内模板；不访问 SSH/API。
- `start`、`status`、`stop`：通过 `--mode` 管理本流水线登记的池与解码服务；支持 `--config`、`--dry-run`。`stop` 可接 `--run-id` 请求停止该次运行。
- `run`、`submit`：需显式提供 `--mode`、`--prompt-file`、`--reference-file`、尺寸、秒数与 `--seed`；可设 `--count`、`--background`、`--config`、`--dry-run`。
- `wait <run_id>`：跟踪本地 run manifest，可设 `--state-dir` 和 `--timeout`。
- `selftest`：离线 mock-HTTP 行为检查。
- `resume <run_id>`：使用本地原始 request/manifest 与远端任务 journal 恢复；支持 `--state-dir`、`--dry-run`，不重新采样。

以上参数已与包内 CLI 对齐。`start` 在启动后检查 `/system_stats` 和模板要求的 `/object_info` 节点；`status` 核对自有进程状态。模型是否能实际执行，最终仍以任务 history 与成片验收为准。

## 启动、提交与正常停止

1. 用 `plan` 先确认所选 mode 与规格属于 [已测范围](run-modes-and-admission.md)。只做离线规划。
2. 准备本地私有 host-map，先 `config-adapt --dry-run` 审阅合并结果，再运行不带 `--dry-run` 的 `config-adapt` 实际生成 `local.json`。该命令拒绝覆盖已存在文件。host-map、适配配置和 run manifest 可能记录实际 IP、目录及 prompt/reference 路径；不得发布包含这些内容的本地日志、配置或 manifest。
3. 启动前检查本地配置字段、route/pool 映射和端口冲突；典型池端口由配置管理（T10 8188/8189、XTX 8299、decoder 8192，另有各池通信端口）。启动前不发远端 API 请求。
4. 先用 `preflight --mode <selected-mode>` 检查静态配置，再运行 `start --mode ...`。启动操作会检查 API 与节点；失败时先排查，不提交新任务。依赖版本快照需要按运行时参考文档单独核对。
5. `run`/`submit` 后记录返回的 `run_id`。用 `wait` 与 `status` 观察 manifest、队列和各任务状态。六任务性能样本不是提交数量上限。
6. 逐任务验收 latent 的 video/audio 张量均 finite；MP4 的分辨率、帧率/帧数、H.264 与 AAC 双声道规格符合任务要求；对每个 MP4 完整执行 `ffmpeg -v error -i <local-file> -f null -` 并要求退出成功。结构门通过不等于人工画质/音质通过。
7. 所有任务完成后，对当前模式执行 `stop`，确认仅本流水线拥有的服务已停止。切换 topology 时，先等任务完成，再 stop 旧模式、start 新模式并重新跑 API preflight。

命令示意（将占位路径换成本机文件；不要把 prompt 或参考图内容写入命令行）：

```sh
python3 scripts/pipeline.py plan --mode eight_plus_one --width 416 --height 736 --seconds 5 --count 1 --seed 1
python3 scripts/pipeline.py config-adapt --host-map /path/to/private-host-map.json --out ~/.config/h3-multigpu-pipeline/local.json --dry-run
python3 scripts/pipeline.py config-adapt --host-map /path/to/private-host-map.json --out ~/.config/h3-multigpu-pipeline/local.json
python3 scripts/pipeline.py start --config ~/.config/h3-multigpu-pipeline/local.json --mode eight_plus_one --dry-run
python3 scripts/pipeline.py start --config ~/.config/h3-multigpu-pipeline/local.json --mode eight_plus_one
python3 scripts/pipeline.py preflight --config ~/.config/h3-multigpu-pipeline/local.json --mode eight_plus_one
python3 scripts/pipeline.py submit --config ~/.config/h3-multigpu-pipeline/local.json --mode eight_plus_one --prompt-file /path/to/local-prompt.txt --reference-file /path/to/local-reference.png --width 416 --height 736 --seconds 5 --count 1 --seed 1
python3 scripts/pipeline.py wait <run_id>
python3 scripts/pipeline.py status --config ~/.config/h3-multigpu-pipeline/local.json --mode eight_plus_one
python3 scripts/pipeline.py stop --config ~/.config/h3-multigpu-pipeline/local.json --mode eight_plus_one
```

## 中断与恢复

- `wait` 超时只表示在限定时间内没有观察到终态；先用 `status` 和 run manifest 判断任务仍运行、已成功、失败或状态不明。不要因客户端断连就直接重复提交整批。
- 保留已完成任务及已通过验收的 latent/MP4；逐项对照 manifest 和输出完整性，只补未完成项。状态不明时先恢复服务可观测性，不要把缺失的日志推断成失败或成功。
- 客户端中断后，先确认旧协调器已结束，再执行 `python3 scripts/pipeline.py resume RUN_ID`。成功项跳过；已提交项依据原 journal/prompt_id 查询；确认保存的 latent 可继续解码；缺失 journal 或无法确认状态的项不会重新提交。历史 watcher 的真实恢复测试和新入口的离线验证分别记录，新入口尚未重跑 GPU 端到端。
- worker 或采样器退出后，保留 run 记录并通过 CLI 管理本包自有服务；不要手工杀其他进程、清理未知目录或盲目重跑。记录具体错误、失败任务 ID 与最后一个成功门，按修复后的配置重新规划未完成项。
- 若需切换 mode，先使旧队列任务全部终止/完成并核对输出，再 stop 旧拓扑，启动新拓扑后重新 preflight。不要让不同 topology 共用同一 GPU、API 端口或 decoder 实例。

统一 CLI 截至此文档编写时只有离线行为验证；上述历史 watcher 恢复和旧路线 GPU 证据不能冒充 CLI 的硬件 E2E 验收。`640×1152 / 15 s` 是已知 fatal OOM，不按相同配置重试。
