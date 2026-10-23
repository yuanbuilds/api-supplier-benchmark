# Kimi K3 供应商自测

> 工具源码采用 [MIT License](LICENSE)；第三方依赖保留各自许可证，见 [第三方声明](THIRD_PARTY_NOTICES.md)。

使用固定方案 `kimi-k3-c1c4-v1`、测试包 **2.3.0** 和 EvalScope **1.13.0**。本次测连通性与小样本性能，供应商先测，采购方再按同口径复验。无需部署网站、安装 Node 或提供 API Key 给采购方。

## 一次启动

需要 Git、Bash、Python 3.10–3.12。Windows 使用 WSL2（未实机验证）。

```bash
git clone https://github.com/yuanbuilds/api-supplier-benchmark.git supplier-benchmark
cd supplier-benchmark
bash start_supplier_test.sh --profile kimi-k3-c1c4-v1
```

按提示填写供应商名称、API 基础地址（通常含 `/v1`，不附加 `/chat/completions`）、精确模型 ID、测试机器地区。入口创建独立的 `supplier-kimi.json`，不会读取旧 GLM 配置。API Key 隐藏输入或从安全环境变量 `SUPPLIER_API_KEY` 注入，不写入文件或命令参数。

先显示完整计划，确认接口、模型权限、额度和时间窗口后输入 `RUN`。其他输入只保存配置，不调用模型。模型 ID 必须与供应商实际接口一致，不从网站显示名猜测。

## 固定计划

| 阶段 | 请求数 | 负载 |
|---|---:|---|
| 连通性 | 1 | 单请求 |
| C1 第一轮 | 10 | 逐条请求 |
| C1 第二轮 | 10 | 逐条请求 |
| C4 | 12 | 最多4个在途请求；总数仍为12 |
| 合计 | **33** | 不预热、不自动重试 |

固定参数：`temperature=1`、`reasoning_effort=low`、`top_p=0.95`、`n=1`、`presence_penalty=0`、`frequency_penalty=0`、`max_tokens=1024`、流式并请求 usage。总超时与读取超时均60秒，连接超时10秒；HTTPS校验，不跟随重定向、不继承系统代理。

输出上限合计 **33,792 Token**，还需计入输入、思考与失败请求计费；这不是预计费用，也没有实时金额熔断。供应商须确认足够额度，不得私自更改参数或删失败样本。

32条性能输入保持2026-10-19公开合成输入的原文件字节，位于 `profiles/kimi-k3-c1c4-v1/`。执行时显式补齐历史平台的有效采样参数并填入本次模型 ID，因此**新请求JSON不等于历史请求字节**。结果保存原文件与编译后输入的各自SHA256，便于核对。

## 报告与回传

入口自动依次预检、dry-run、执行，打印 **report.html** 和唯一回传 **ZIP**。先用 Chrome 打开报告，再把该 ZIP 原样发回；失败也保留并回传，不手工拼截图。ZIP只保留状态、时间、usage、合成请求、配置和复验所需证据，不保留上游原始回答。程序检查 Key 残留后才打包。

完整成功要求 HTTP 200、非空正文、`finish_reason=stop`、`[DONE]`；空回复、仅思考、截断、限流、超时均失败。TTFT 到首个非空思考或正文，TTFOT 到首个非空正文；均从 HTTP 调用前计时，包含建连，排除调度等待。失败单列，成功样本统计P50/P95/P99。

**本方案没有1秒或成功率SLA门槛，也没有200 RPM阶段。** 正常完成后总评为 `measurement_only`；存在失败则如实显示，阶段全失败或证据不完整将停止后续阶段。33次不证明长期SLA、最大供量、模型身份、计费正确性或工具/视觉等功能。

供应商自测只说明其测试机器到接口的表现。采购方复验时须保持输入、参数、软件版本一致，并记录地区、网络和直连/平台路径；不同路径或不同日期不能直接归因于模型质量。

## 常用命令

```bash
# 已配置：仅查看计划，不访问供应商
bash start_supplier_test.sh plan --profile kimi-k3-c1c4-v1

# 已确认额度与窗口：实际执行33次
bash start_supplier_test.sh run --profile kimi-k3-c1c4-v1 --execute

# 完整本地模拟33次，不消耗供应商额度；不能冒充Kimi实测
bash start_supplier_test.sh demo --profile kimi-k3-c1c4-v1
```

可用 `--config 路径` 指定独立配置；模板为 `profiles/kimi-k3-c1c4-v1/supplier.example.json`。不要执行不带profile的GLM向导代替本方案。遇到安装失败，回传最后一段错误及系统/Python版本，不附带环境变量、Key或其他供应商资料。
