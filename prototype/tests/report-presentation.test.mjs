import assert from 'node:assert/strict'
import test from 'node:test'

import { buildReportEventMap } from '../src/reportPresentation.js'

const REPORT_FIXTURE = `# 离开、面试与一道没讲明白的数学题：你这一天真正需要闭环的三件事

## 核心结论

这一天有三条主线：职业选择、电话面试和晚间的数学辅导。

## 重要时间轴

1. 午餐中段：讨论职业选择与理想组织问题。
2. 午后：完成电话面试，但后续尚未确认。
3. 晚间：数学辅导时孩子没有听懂原来的讲法。

## 工作与职业选择

### 用评分表比较机会

把组织、角色和成长空间写成统一评分表。

## 关键面试

### 主动闭环后续

在约定时间内确认下一步。

## 亲子辅导

### 换一种讲法

先让孩子复述理解，再用具体例子重讲。`

test('builds an event map from a representative DeepSeek report without inventing clock times', () => {
  const presentation = buildReportEventMap(REPORT_FIXTURE)

  assert.equal(presentation.title, '离开、面试与一道没讲明白的数学题：你这一天真正需要闭环的三件事')
  assert.equal(presentation.events.length, 3)
  assert.deepEqual(presentation.events.map((event) => event.phase), ['午餐中段', '午后', '晚间'])
  assert.match(presentation.events[0].event, /职业选择|理想组织问题/)
  assert.match(presentation.events[0].improvementTitle, /评分表/)
  assert.match(presentation.events[1].event, /电话面试/)
  assert.match(presentation.events[1].improvementTitle, /闭环/)
  assert.match(presentation.events[2].event, /数学辅导/)
  assert.match(presentation.events[2].improvementTitle, /换一种讲法/)
  assert.ok(presentation.events.every((event) => !/\b\d{1,2}:\d{2}\b/.test(`${event.phase} ${event.event}`)))
})

test('returns no event map when a report does not contain enough grounded event and improvement pairs', () => {
  const presentation = buildReportEventMap('# 一天的报告\n\n## 核心结论\n\n今天有一些事情发生。')

  assert.equal(presentation, null)
})
