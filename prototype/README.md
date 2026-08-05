# Audio Memory 可交互原型

这是 Audio Memory 第一阶段的全屏可交互原型。除 API Key 真实校验、本地 Whisper 转写和模型内容分析外，核心产品流程均可直接操作。

## 启动

```bash
cd "/Users/liujinxin/Documents/音频Always on Demo/prototype"
npm install
npm run dev
```

打开终端输出的本地地址即可使用。

## 原型模拟规则

- 任意非空 API Key 模拟校验成功；包含 `invalid` 的 Key 模拟校验失败。
- 选择 MP3/AAC 文件后模拟逐文件上传。
- 文件名包含 `transcription-fail` 时模拟 Whisper 转写失败。
- 文件名包含 `analysis-fail` 时模拟模型分析失败。
- 转写或分析中刷新页面，可查看中断任务恢复。

`fixtures/` 中提供了用于验证上述状态的模拟文件。
