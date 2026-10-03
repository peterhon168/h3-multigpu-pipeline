---
name: h3-multigpu-pipeline
description: "Run and recover the packaged MiniMax H3 multi-GPU production pipeline across Tesla T10 CUDA sampler pools, optional RX 7900 XTX ROCm sampling, and RTX 3080 Ti decoding. Use when selecting a validated topology, adapting a local host map, planning or submitting jobs, checking services, or recovering a run."
---

# H3 多 GPU 生产流水线

使用本目录内的 `scripts/pipeline.py`、`config/`、`workflows/` 和 `assets/` 运行隔离的采样池与解码队列。按下表选择拓扑；规格准入和性能证据见 [运行模式与准入](references/run-modes-and-admission.md)。

| `--mode` | 设备拓扑 | 适用场景 |
| --- | --- | --- |
| `eight_plus_one` | 8×T10 + 3080 Ti | 单条、首片与已验证长规格优先 |
| `four_four_plus_one` | 两组互斥 4×T10 + 3080 Ti | 416×736 短片批量默认 |
| `four_four_xtx_plus_one` | 两组 4×T10 + XTX + 3080 Ti | 736×1280 批量性能候选，人工审片通过前不自动升为默认；低档仅可选 |
| `eight_xtx_plus_one` | 8×T10 + XTX + 3080 Ti | 必须保留八卡单池时的特殊低档候选，不作为默认批量路线 |

## 操作顺序

1. 阅读 [运行模式与准入](references/run-modes-and-admission.md)，再用 `plan` 离线确认规格、数量和模式。
2. 用私有 host-map 运行 `config-adapt --dry-run`，审阅后去掉 `--dry-run` 生成本地配置；运行 `preflight --mode MODE` 检查本地配置与模板。该命令不访问 SSH/API。
3. 对本流水线自有服务运行 `start --mode MODE`。该操作检查服务所有权、端口、启动后的 ComfyUI API 与必需节点；成功后再提交任务。源码/依赖快照是复现参考值，不是 `preflight` 自动核验的结果。
4. 用 `run` 或 `submit` 显式提供本地 prompt/reference 文件；用 `wait`、`status` 跟踪 manifest，逐条通过 latent 数值、视频/音频结构和完整解码验收后再交付。
5. 切换模式前先等当前任务全部结束，再停止旧模式的自有服务并启动新模式。停止时只操作本包登记的服务。

详细命令、异常恢复和停止/切换次序见 [运行与恢复](references/operations-and-recovery.md)。模型、运行时、传输与补丁约束见 [运行时与资产](references/runtime-and-assets.md)。

`count` 不受六条限制：六条只是性能采样批量。新统一 CLI 通过离线行为验证；历史 GPU 路线有实测，新入口尚未重新跑真实硬件端到端。`resume RUN_ID` 只恢复已提交任务或已保存 latent 的解码，不重新采样；状态不明时保留记录并停止重提。
