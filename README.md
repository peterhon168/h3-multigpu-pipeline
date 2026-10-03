# MiniMax H3 多卡采样 / 单卡解码生产流水线

Tesla T10 多卡采样池、可选 RX 7900 XTX 采样源，共享一张 RTX 3080 Ti 解码。提供 Agent Skill、运行脚本、工作流模板、兼容补丁和可复现报告。

**当前状态：封装与离线验证已完成。历史路线有真实 GPU 实测，但新的统一 CLI 尚未补跑真实硬件端到端及断线恢复验收。** 高档三源仍需人工画质/音质审核；不要把模拟测试当作硬件生产验收。

## 根据任务选模式

| `--mode` | 拓扑 | 用途 |
|---|---|---|
| `eight_plus_one` | 8×T10 + 3080 Ti | 单条、首片、已验证长规格 |
| `four_four_plus_one` | 4+4×T10 + 3080 Ti | 416×736 / 五秒批量默认 |
| `four_four_xtx_plus_one` | 4+4×T10 + XTX + 3080 Ti | 736×1280 / 五秒批量性能候选；待人工审片 |
| `eight_xtx_plus_one` | 8×T10 + XTX + 3080 Ti | 需要保留八卡单池时的可选路线；非默认 |

仅 `eight_plus_one` 有 736×1280 / 十秒和 544×960 / 十五秒的本轮证据。640×1152 / 十五秒已发生 fatal OOM，不准入。五秒为 124 帧、24 fps，实际容器时长约 5.167 秒。

## 开始使用

在准备好模型与独立 ComfyUI/CUDA/ROCm 环境后，阅读 [工具包说明](h3-multigpu-pipeline/README.md) 和 [Skill](h3-multigpu-pipeline/SKILL.md)。本仓库不是驱动、系统环境或模型的一键安装器。

```sh
git clone https://github.com/peterhon168/h3-multigpu-pipeline.git
cd h3-multigpu-pipeline/h3-multigpu-pipeline
python3 scripts/pipeline.py plan --mode eight_plus_one --width 416 --height 736 --seconds 5 --count 1 --seed 1
python3 scripts/pipeline.py selftest
```

填写自己的私有 host-map，再执行 `config-adapt`、静态 `preflight`、`start` 和 `submit`。启动前确认所选设备空闲。其他 agent 可把 `h3-multigpu-pipeline/` 安装到其 skills 目录并调用 `$h3-multigpu-pipeline`。

入口包含：`plan`、`config-adapt`、`preflight`、`start`、`submit/run`、`wait`、`status`、`stop`、`resume`、`selftest`。支持双池隔离、有限背压、单解码队列、SHA-256 原子交接、10GbE/LAN 回退、服务所有权和 journal 恢复；未知状态不自动重采样。

## 报告与验证

- [2026-10-03 完整复现报告](FORUM_REPRODUCTION.md)：先总览，再按调优轮次解释变更、收益和限制。
- [结构化结果](results.json)：七组单项/容量数据、八组批量数据及计时定义。
- [Release 下载](https://github.com/peterhon168/h3-multigpu-pipeline/releases)：独立 Skill zip 和论坛全套 zip。

本地通过 13 项 mock selftest、八项独立复核测试、Python 编译、Skill 校验、压缩包 CRC/SHA-256 和脱离原工程的四模式离线计划。新入口增加完整 CPU 解码验收；与历史 marker 计时不能直接混用。

## 隐私与第三方来源

不包含权重、原始视频/参考图、私人 prompt、内网 IP、密钥或本机配置。自己的 host-map、配置、manifest 和运行日志请保留在仓库外，不要提交。公开模板使用通用示例，不能保证复现原始私有参考图的绝对耗时。

本包包含不同许可证的第三方资产，见 [来源和许可证说明](THIRD_PARTY_NOTICES.md)。发布不改变上游权重及其使用条款。
