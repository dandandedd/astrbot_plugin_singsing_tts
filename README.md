# SingSing 主动语音（LLM 工具版）

Bot 可以在回复阶段通过同一个 LLM 工具 `send_voice(text, style)` 主动选择普通语音或唱歌。插件只负责工具调用后的 TTS 合成与消息发送，不再通过正文标签、System Prompt 或结果链钩子控制语音。

## 适配范围

- 仅适配 **AstrBot 4.29.4**。
- 仅支持 QQ 的 **`aiocqhttp`** 协议端。
- 不兼容其他 AstrBot 版本，也不声明支持其他消息平台。
- 插件只使用当前会话选定的 TTS Provider，不内置 TTS 服务。
- TTS 兼容性说明：目前仅在 Fish Audio Drama3 模型下完成测试，其他 TTS 服务商的兼容性尚未验证。
- 免责声明：受限于底层 TTS 模型本身的能力，部分歌曲可能无法生成演唱效果（从而转为普通朗读），此现象属于上游服务限制，非本插件缺陷。

## 工作方式

插件只注册一个工具：

```python
send_voice(text, style)
```

模型根据对话需要主动调用：

- `style="speech"`：普通说话。TTS Provider 收到的文本就是清理后的 `text`。
- `style="sing"`：唱歌。TTS Provider 收到的文本是：

  ```text
  <singing_prefix><text>
  ```

  默认等价于：

  ```text
  [singsing]窗外的风轻轻吹过
  ```

`style` 大小写不敏感，并会去除首尾空白。非法值安全归一化为 `speech`。

`[singsing]` 只是插件内部提供给 TTS Provider 的唱歌标记，不是聊天协议，也不需要模型输出。模型只填写纯正文 `text`，插件负责在唱歌时追加该前缀。该前缀不会出现在模型输出、用户可见聊天正文或 `Record.text` 中。

### 示例

普通语音：

```text
send_voice(text="今天辛苦啦，早点休息。", style="speech")
```

唱歌：

```text
send_voice(text="窗外的风轻轻吹过，我把今天唱给你听。", style="sing")
```

上述工具调用由模型在 Agent Loop 内完成，用户不需要手动输入工具名或任何控制标记。

## 普通语音与唱歌的 TTS 输入

| 模式 | Provider 收到的 TTS 文本 | `Record.text` / 用户可见正文 |
| --- | --- | --- |
| `speech` | 清理后的纯正文 | 清理后的纯正文 |
| `sing` | `singing_prefix` + 清理后的纯正文 | 清理后的纯正文 |

插件会循环清除正文中的 `singing_prefix` 和固定 `[singsing]` 标记，直到不再出现；因此即使模型误传了控制标记，也不会把它们发送给用户或写入 `Record.text`。唱歌需要 Provider 识别 `singing_prefix`；不识别该前缀的 Provider 可能按普通文本朗读。

> 警告：不要把 `singing_prefix` 配置成 `sing`、`唱` 等常见短词。插件按字面子串清理该前缀，短词可能误删正常歌词或普通正文。建议保留默认的 `[singsing]`，或使用同样具有明确边界的控制 token。

## 环境要求

- AstrBot `4.29.4`。
- QQ `aiocqhttp` 协议端已连接并正常工作。
- AstrBot 已启用 TTS，并配置了可用的 TTS Provider。
- 当前会话没有关闭 TTS。
- TTS Provider 返回非空、存在且字节数大于 0 的本地音频文件，或合法的 `file://` URI。
- 建议关闭 AstrBot 流式输出，详见后文。

插件没有第三方 Python 运行时依赖。

## 安装

将插件目录放到 AstrBot 数据目录：

```text
data/plugins/astrbot_plugin_singsing_tts/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── README.md
└── LICENSE
```

然后在 AstrBot WebUI 的插件管理中重载插件。重载后应能在 LLM 工具列表中看到 `send_voice`。

## 必需配置

1. 在 AstrBot 中开启全局 TTS。
2. 配置并选择至少一个可用的 TTS Provider。
3. 确认当前会话的 TTS 开关处于开启状态。
4. 建议将 AstrBot 核心 TTS 的 `trigger_probability` 设置为 `0`。
5. 建议关闭流式输出。
6. 建议关闭工具状态显示与工具结果显示，避免 `send_voice` 调用产生额外可见状态消息。
7. 检查 Core 的 `agent_runner.config.misc.tool_call_timeout`，确保它明显大于预期的 TTS 合成时间。
8. 若 Provider 依赖 `[singsing]` 触发唱歌，确认该 Provider 确实支持这一输入约定。

插件主动调用 TTS 不依赖 `trigger_probability`。将该概率设为 `0`，是为了避免普通文字回复被 AstrBot Core 随机转成语音；它不会阻止 `send_voice` 工具按本次调用确定性地合成语音。

插件不会修改 AstrBot 全局配置，也不会绕过全局或会话级 TTS 开关。

## 插件配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `singing_prefix` | string | `[singsing]` | 仅在 `style="sing"` 时追加到 Provider 输入开头。该值不能为空；空白或非法类型会回退到默认值。 |
| `tts_timeout_seconds` | float | `30.0` | `provider.get_audio()` 的超时时间，允许范围 1-300 秒。非法值回退到 30；越界值夹紧到边界。 |
| `max_text_chars` | int | `1000` | 清理后正文的最大字符数，允许范围 1-10000。超长时不调用 TTS，直接发送纯文字。 |

### Core 外层工具超时

本插件工具由 AstrBot 4.29.4 的 local runner 执行，因此会读取当前会话的：

```text
agent_runner.config.misc.tool_call_timeout
```

缺失或非法时按 120 秒处理。插件会在 Core 外层取消前为文字兜底预留 2 秒：

```text
effective = min(tts_timeout_seconds, max(0.1, outer_timeout - 2.0))
```

因此实际等待 Provider 的时间可能小于配置的 `tts_timeout_seconds`：

- `outer_timeout <= 2.0`：不调用 TTS，直接发送纯文字并结束本轮。
- `outer_timeout > 2.0`：使用上述 `effective` 包裹 `provider.get_audio()`，确保插件能在 Core 外层超时前完成 Plain 兜底。

## 发送与兜底规则

- 工具成功发送语音后返回 `None`。AstrBot 4.29.4 的 Tool Loop 会结束本轮 Agent Loop，不会继续生成另一段普通回复。
- 工具 docstring 明确要求模型调用时不要在普通正文中重复语音内容；handler 入口会先清理待发送结果。支持的 4.29.4 非流式完成路径中，最终只发送一条语音或一条 Plain 兜底，不会再把同响应的 `completion_text` 交给 `RespondStage` 重复发送。
- 同一消息事件最多只产生一次 `send_voice` 输出。后续同事件调用会被忽略，避免重复语音或重复文字。
- `text` 为 `None`、空字符串或清理后为空时静默返回 `None`，不发送任何内容。
- `text` 不是字符串时记录警告并返回 `None`，不会隐式调用 `str()` 朗读 Python 对象。
- 非 `aiocqhttp` 平台不参与语音发送；插件会直接发送清理后的普通文字并结束本轮。
- 以下情况都会直接发送清理后的 `Plain` 文字并结束本轮，不触发下一轮 LLM：
  - 全局 TTS 关闭；
  - 会话 TTS 关闭；
  - TTS Provider 缺失；
  - TTS 合成超时或抛出异常；
  - Provider 返回空路径、不存在的文件、目录、0 字节文件、非本地 URI 或非法 `file://` URI；
  - 音频消息发送抛出异常。
- 若发送纯文字兜底也失败，插件只记录异常日志，不向 Agent Loop 抛出未处理异常。
- 普通文字回复不会经过插件处理，也不会被插件改写。

## 流式输出限制

插件不支持流式输出。请关闭 AstrBot 流式模式后再使用本插件。流式链路可能在工具调用或最终语音发送之前已经向用户输出文本，本插件不负责回收或去重这类已发送内容。

同时建议关闭 `show_tool_use_status` 和工具结果显示。开启这些选项后，AstrBot 可能额外发送“调用工具: send_voice”等状态 Plain，虽然那不是插件生成的第二段语音，但会破坏“一轮只看到一条用户可见消息”的预期。

## 失败时为什么收到纯文字

TTS 是可选增强能力。为了保证用户至少能收到回复，插件在 TTS 不可用、Provider 失败、超时、音频无效或语音发送失败时统一回退到清理后的纯文字。这是预期行为。

## 故障排查

1. 确认 AstrBot 版本是 4.29.4。
2. 确认当前平台是 `aiocqhttp`。
3. 确认插件已加载，且工具列表中存在 `send_voice`。
4. 确认全局与会话 TTS 均已开启。
5. 确认已选定可用的 TTS Provider。
6. 确认模型确实调用了 `send_voice`，而不是直接输出普通文本。
7. 确认 Provider 返回的本地音频文件存在且非空。
8. 检查 AstrBot 日志中的 TTS Provider、音频路径、超时和 `aiocqhttp` 发送错误。
9. 若唱歌只是普通朗读，确认当前 Provider 支持 `singing_prefix` 对应的唱歌模式。

平台真实发送仍需实机验证 TTS Provider、`aiocqhttp` 连接、音频格式和 QQ 协议端权限。

## 隐私

- 工具参数中的正文会发送给当前 TTS Provider。
- 唱歌时，Provider 还会收到插件追加的 `singing_prefix`。
- 插件日志不主动记录完整正文；异常日志可能包含 Provider 返回路径、异常类型或平台错误。
- TTS Provider、AstrBot Core 和 `aiocqhttp` 适配器自身的数据处理与日志行为不在本插件控制范围内。

## 许可证

本插件采用 MIT License，版权归 `dandandedd` 所有。AstrBot 本体遵循其自身许可证，二者边界以各自仓库许可证为准。
