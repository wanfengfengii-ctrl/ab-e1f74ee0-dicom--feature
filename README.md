# Strict DICOM Frame Extraction Service

数字病理归档用的**零容错** DICOM Part 10 取帧服务。它从多帧切片中取出指定的
**原始压缩帧**，逐字节原样返回，绝不依赖通用解析器的“容错”逻辑，因此不会掩盖
偏移错位、截断或封装结构损坏。

- 接口：`POST /api/dicom/frames`（`multipart/form-data`）
- 健康检查：`GET /health`
- 唯一接受的像素编码：JPEG Baseline（Transfer Syntax `1.2.840.10008.1.2.4.50`）
- 唯一接受的数据集编码：Explicit VR Little Endian（Part 10 文件，带 `DICM` 前导）

## 请求

`multipart/form-data`，包含：

| 字段 | 形式 | 约束 |
| --- | --- | --- |
| `file` | 一个文件 Part | Part 10 文件，**不超过 16 MiB** |
| `frames` | 一个或多个文本 Part | 1–32 个**互异的零基帧号**；单个 Part 内可用逗号分隔，多个 Part 按出现顺序收集 |

声明帧数（Number of Frames, `(0028,0008)`）必须在 `1..256`。

示例：

```bash
curl -s http://localhost:8080/api/dicom/frames \
  -F 'file=@slide.dcm;type=application/dicom' \
  -F 'frames=2' -F 'frames=0'
```

## 响应

按**请求顺序**返回每帧，载荷为各片段**原样拼接**后的 Base64：

```json
{
  "number_of_frames": 4,
  "frames": [
    {
      "index": 2,
      "fragment_count": 3,
      "byte_count": 105182,
      "sha256": "9f2a…",
      "data": "…base64…"
    }
  ]
}
```

- `fragment_count`：该帧由多少个封装片段（item）拼接而成。
- `byte_count`：拼接后（Base64 解码前的原始字节数）。
- `sha256`：拼接后原始字节的 SHA-256 十六进制摘要。
- 结果对同一文件稳定且逐字节一致（重复请求完全相同）。

## 严格校验规则

任何不符都返回稳定错误类型，**绝不返回部分帧**（错误响应只有 `error` 对象）：

- 128 字节前导 + `DICM` 魔数；
- File Meta Information：首元素为 `(0002,0000)` UL，声明长度精确落在元素边界；
- Transfer Syntax 必须是 JPEG Baseline；
- 主数据集 Explicit VR Little Endian，元素 tag 严格递增、值长度有限且为偶数、
  边界全部核对，无尾随数据，仅有一个 Pixel Data；
- Pixel Data 为未定义长度的 OB 封装流，项目长度合规（偶数且 ≥ 2），
  以长度为 0 的 Sequence Delimitation Item（`FFFE,E0DD`）结束；
- **完整 Basic Offset Table**：首项为 0、严格递增、条目数等于声明帧数、
  每项都落在片段项目边界上；越界 BOT、BOT 与片段间填充一律拒绝；
- **超大切片的 Extended Offset Table 备选方案**：超大数字病理切片可以使用
  **空 Basic Offset Table**（BOT item 长度为 0）配合成对出现的
  `(7FE0,0001) Extended Offset Table` 与
  `(7FE0,0002) Extended Offset Table Lengths`。两张扩展表必须：
  - 都位于 Pixel Data **之前**，使用 **Explicit VR Little Endian、VR 为 `OV`**，
    值长度为 8 的倍数且非空；
  - 成对出现，条目数等于声明帧数（两张表条目数也必须相同）；
  - 偏移为 64 位小端、自空 BOT 后首个片段 item tag 起算，**首项为 0、严格递增、
    每项都落在片段项目边界上**；
  - 长度为对应帧覆盖的**完整片段载荷范围**（各片段 payload 之和，不含 item 头）。

  扩展表**不得与非空基础偏移表混用**；空 BOT 若无扩展表仍按歧义拒绝；
- 每帧拼接后必须是结构合法的 Baseline JPEG：SOI/EOI、唯一 `SOF0`、
  合法标记段与 SOS 扫描（熵数据中的 `FF00` 填充与 `RSTn` 正确识别）。
  按 PS3.5 6.2/A.4，每帧最后一个片段允许在 EOI 后恰好一个 `0x00` 偶对齐填充，
  该字节作为原始载荷的一部分原样保留。

### 错误类型

| HTTP | `error.type` | 含义 |
| --- | --- | --- |
| 400 | `FILE_MISSING` | 缺少 `file` 字段 |
| 400 | `MALFORMED_REQUEST` | multipart 结构/Content-Type 错误 |
| 400 | `NO_FRAME_INDICES` | 未提供帧号 |
| 400 | `TOO_MANY_FRAME_INDICES` | 帧号超过 32 个 |
| 400 | `DUPLICATE_FRAME_INDEX` | 帧号重复 |
| 400 | `INVALID_FRAME_INDEX` | 帧号不是非负十进制整数，或 ≥ 256 |
| 413 | `FILE_TOO_LARGE` | 文件/请求体超过 16 MiB 上限 |
| 422 | `FRAME_INDEX_OUT_OF_RANGE` | 帧号超出文件实际帧范围 |
| 422 | `INVALID_PREAMBLE` | 前导/魔数错误或被截断 |
| 422 | `INVALID_METADATA` | 元信息长度/元素/Transfer Syntax 缺失等 |
| 422 | `UNSUPPORTED_TRANSFER_SYNTAX` | 非 JPEG Baseline |
| 422 | `MALFORMED_ELEMENT` | 数据集元素非法（隐式 VR、未定义长度、tag 乱序、奇长度等） |
| 422 | `TRUNCATED_DATA` | 任何头部、值、项目或结束标记被截断 |
| 422 | `INVALID_FRAME_DECLARATION` | Number of Frames 缺失/非法/超出 1..256/与 BOT 不符 |
| 422 | `PIXEL_DATA_STRUCTURE` | Pixel Data 封装结构、项目或结束标记错误 |
| 422 | `INVALID_BASIC_OFFSET_TABLE` | BOT 首项非 0/非递增/不对齐/数量不符，或空 BOT 且无扩展表 |
| 422 | `INVALID_EXTENDED_OFFSET_TABLE` | 扩展表缺失/不成对/非 OV、与非空 BOT 混用、条目数不符、偏移首项非 0/非递增/越界/不对齐，或长度与片段范围矛盾 |
| 422 | `INVALID_JPEG_STREAM` | 帧不是合法 Baseline JPEG |
| 422 | `UNEXPECTED_TRAILING_DATA` | Pixel Data 之后存在尾随字节 |

## 本地运行（无需 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
APP_PORT=8080 .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Docker 交付

`APP_PORT` 配置宿主机发布端口（默认 8080），容器内健康检查访问 `/health`。

```bash
# 启动 API
APP_PORT=8080 docker compose up --build api

# 一次性校验：等待 API 健康 -> pytest 代码测试 -> 应用构建检查
#   -> 合法文件（含多片段帧、含 Extended Offset Table）与坏偏移表
#      （BOT / 扩展表长度矛盾）的接口冒烟，以退出码报告后退出
APP_PORT=8080 docker compose up --build \
  --abort-on-container-exit --exit-code-from verify
echo "verify exit code: $?"
```

## 测试

```bash
.venv/bin/python -m pytest -q
```

测试包含与 `pydicom` 生成的真实 Part 10 文件的逐字节交叉验证
（BOT 语义、多片段分组、奇数帧偶对齐填充）。
