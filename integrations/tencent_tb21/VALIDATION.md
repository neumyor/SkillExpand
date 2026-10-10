# Pipeline 缓存验收：2026-10-10

已在 jinan40 部署；缓存版本 `pipeline-uv095-py3139-v6` 状态为 `ready`。
默认开关仍为关闭，未启动模型实验或补跑，也未修改现有实验进程。

## 后续正式补跑验收

上述默认关闭是初次交付快照。2026-10-10 经用户授权，E5 `81-1` 请求
017 显式启用了此版本，正式返回真实 verifier reward 0，无 trial exception。
缓存传输 809.43 秒、解包 18.04 秒，准备合计 827.57 秒；verifier
86.82 秒完成，pytest 为 2 passed / 2 failed（33.95 秒）。正常 stdout
存在，下载遥测为空。这个真实 0 分计入该槽位；工程验证的 0 分从未导入。
完整执行审计通过后 E3/E5/E6 均已进入 L2，演化仍在进行。

## 实际交付

- 入口：`/data2/liyishan/tb21-tencent-skill/python/run_tencent_job.py`。
- 缓存目录：`/data2/liyishan/tbench2-openclaw-min/cache/pipeline/pipeline-uv095-py3139-v6`。
- 原任务镜像：`modelbest.tencentcloudcr.com/terminalbench/runtime:torch-pipeline-parallelism_20251031`。
- uv 0.9.5；CPython 3.13.9，官方 20251014 构建，Linux x86_64 GNU。
- 包含全部 45 个实际解析包、版本约束和来源记录。
- 376 个分块，3,147,399,198 字节（约 2.93 GiB）；展开依赖材料约 3.08 GiB。
- 为减少传输体积，完整 wheelhouse 作为可移植包缓存；不重复打包其解压副本。新沙箱离线重建 uv 安装缓存。

## 同镜像全新沙箱验证

| 阶段 | 实测 |
|---|---:|
| 8 MiB 串行上传 | 739.64 秒 |
| 解包与发布 | 20.54 秒 |
| 注入总计 | 760.28 秒，低于 1,200 秒 |
| 离线 pytest 启动、torch/transformers 导入及版本比对 | 51.07 秒 |
| 重复准备 | 0.09 秒，无重复传输 |
| 未修改的评分脚本 | 51.89 秒，低于 900 秒 |
| 其中 pytest 测试执行 | 16.04 秒 |
| 评分脚本下载 wheel | 0 |

45 个包版本完全匹配；原脚本命中 uv 本地安装器并进入 pytest。
工程沙箱没有 agent 解题产物，4 项测试失败，真实 reward 为 0。
这个结果没有导入 E3/E5/E6 或任何正式评分槽位。

注入、离线验证和原脚本合计约 863 秒。此前独立冷下载在 1,001 秒时仍未完成，
因此这里只使用它作为冷准备耗时下界，不将其写成完整的对照运行，也不承诺所有请求
都获得相同加速。缓存构建本身共约 1,413 秒；这是一项一次性成本。

## 验证范围

- 8 项专项测试通过：关闭开关/非目标任务、未验证版本、缺失/不完整分块、
  三次传输尝试后失败、准备超时、重复准备、agent 环境隔离、HTTP 分段发布。
- 14 项现有 Snapshot/RetryArchive 测试通过。
- 已在安装的 Harbor/Tencent 类上验证环境注册及 `VERIFICATION_START` hook。
- 当前验证沙箱与所有前置准备沙箱均已删除；失败候选保持 `failed`。
- 未改评分脚本、模型请求预算、verifier 超时或原超时进程清理机制。

## 后续新请求的显式启用

```sh
export TB21_PIPELINE_CACHE=1
export TB21_PIPELINE_CACHE_VERSION=pipeline-uv095-py3139-v6
```

仅在已有授权的新请求中传入上述变量。当前交付不自动启动请求；默认关闭仍然有效。
回退只需将 `TB21_PIPELINE_CACHE=0`。缓存版本清单和完整验证结果见 `evidence/`。
