# H3 多卡采样、单卡解码工具包

给其他 agent：先完整阅读 [SKILL.md](SKILL.md)，按已测规格选模式，不自行升级模型、steps、attention 或精度。调用示例：`使用 $h3-multigpu-pipeline，按本地配置生成……`。在现有 ComfyUI/模型环境上使用；不是自动安装驱动、ROCm、CUDA 或权重的一键装机包。

## 三步开始

1. 将本目录安装到 agent 的 skills 目录，或直接从此目录运行脚本。阅读 `references/runtime-and-assets.md`，在停机的隔离 ComfyUI 中准备模型、补丁及 `assets/` 节点。
2. 复制 `config/host-adapter.example.json` 到本地私有目录，填自己的 SSH 目标、绝对路径、设备号与传输目标。XTX/decoder 同机时明确设 `same_host_as_decoder: true`。随后执行：

   ```sh
   python3 scripts/pipeline.py config-adapt --host-map /path/to/private-host-map.json --out /path/to/private-local.json
   python3 scripts/pipeline.py preflight --config /path/to/private-local.json --mode eight_plus_one
   ```

3. 离线规划后启动、提交：

   ```sh
   python3 scripts/pipeline.py plan --mode eight_plus_one --width 416 --height 736 --seconds 5 --count 1 --seed 1
   python3 scripts/pipeline.py start --config /path/to/private-local.json --mode eight_plus_one
   python3 scripts/pipeline.py submit --config /path/to/private-local.json --mode eight_plus_one --prompt-file /path/to/prompt.txt --reference-file /path/to/reference.png --width 416 --height 736 --seconds 5 --count 1 --seed 1 --background
   python3 scripts/pipeline.py wait RUN_ID
   ```

`start/run/submit/resume/stop` 可访问远端服务；`plan/config-adapt/preflight` 不访问显卡。先确认相关设备空闲、环境准备完成，再启动。输出文件保留在解码主机配置的 `run_root/services/decoder/output/`，实际路径记录在私有 run manifest。

异常恢复、停止、切换模式见 `references/operations-and-recovery.md`。测试过的规格与模式边界见 `references/run-modes-and-admission.md`。`selftest` 仅本地模拟，不是 GPU 验收。

## 交付边界

包内含启动/提交/等待/恢复脚本、三个工作流模板、两份兼容补丁、A6 CLIP 与 latent 保存/加载节点。公开文件不含权重、个人参考图、原始 prompt、私钥、真实主机地址或运行日志。性能数值来自历史实测；新统一入口目前只有离线行为验证，需要在空闲时补一次真实硬件端到端验收。
