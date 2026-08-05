const FORMAT_ERROR = '不支持该文件格式，请上传 MP3 / AAC 格式文件';

export async function validateProviderKey(providerId, key) {
  await new Promise((resolve) => setTimeout(resolve, 30));
  if (!['kimi', 'deepseek', 'openai'].includes(providerId)) return { ok: false, message: '不支持的模型厂商' };
  if (!key.trim()) return { ok: false, message: '请填写 API Key' };
  if (key.toLowerCase().includes('invalid')) return { ok: false, message: 'API Key 不可用，请检查后重新填写' };
  return { ok: true, message: '连接可用' };
}

export function acceptAudioFile(fileLike) {
  const extension = fileLike.name.split('.').pop()?.toLowerCase();
  if (!['mp3', 'aac'].includes(extension)) return { ok: false, error: FORMAT_ERROR };
  return { ok: true, extension: extension.toUpperCase() };
}

const transcript = `10:12 今天主要评审 Always-on Demo 的第一期范围。\n10:24 先用本地 Whisper 统一转写，再比较三个模型的内容分析效果。\n10:43 下周二前需要完成可交互原型。\n17:32 孩子因为数学作业卡住而哭闹，家长先追问了几次为什么不会。\n18:06 这个不打断用户的记忆层思路很有价值。`;

export function buildMockBatch(files, providerId, prompts = {}) {
  const id = `batch-${Date.now()}`;
  const audio = files.map((file) => ({ name: file.name, size: file.size ?? 0 }));
  const cards = [
    {
      id: `${id}-meeting`, sceneId: 'meeting', label: '会议纪要', timeLabel: '10:12–11:03',
      title: 'AI 眼镜 Always-on 产品方案评审',
      summary: '第一期先验证 PC Demo 的高价值信息与展示方式，暂不接入眼镜实时录音。',
      meta: '2 个明确决策　 3 项会议待办',
      detailSections: [
        { title: '会议摘要', content: '团队围绕 Always-on 第一阶段范围、模型处理方式和本地数据策略达成共识。' },
        { title: '核心结论', items: ['第一期只处理用户主动上传的已有音频', '三个厂商统一使用本地 Whisper 转写', '音频、转写和生成结果保存在用户本机'] },
        { title: '明确决策', items: ['首发支持 macOS Apple Silicon', '原始音频不发送给分析模型'] },
        { title: '会议待办', items: ['下周二前完成可交互原型', '补充 Prompt 管理结构', '整理所有异常状态'] },
      ],
    },
    {
      id: `${id}-parenting`, sceneId: 'parenting', label: '家庭教育', timeLabel: '多个互动片段',
      title: '孩子遇到挫折时，先接住情绪再讲道理',
      summary: '孩子在作业受挫后明显烦躁，家长的连续追问让对话短暂升级。', meta: '本次上传',
      detailSections: [
        { title: '背景信息', content: '晚饭后辅导数学作业时，孩子因为连续做错两道题而哭闹。' },
        { title: '找出问题所在', content: '孩子当时可能处在挫败和紧张状态；连续追问原因增加了回答压力。情绪原因为根据对话的合理推测。' },
        { title: '给出切实建议', items: ['先说“这道题卡住了，你现在有点着急”', '等情绪稳定后，请孩子指出从哪一步开始不确定', '每次只处理一个小步骤'] },
      ],
    },
    {
      id: `${id}-content`, sceneId: 'content', label: '内容推荐', timeLabel: '本次上传',
      title: '你这次关注了 AI 产品、表达和个人成长',
      summary: '共识别 4 项内容，主要围绕 AI 产品设计、结构化表达与个人效率。', meta: '4 项内容',
      detailSections: [
        { title: '这次你都听了', items: ['AI 眼镜 Always-on 产品讨论：如何在不打断用户的情况下创造价值', '《金字塔原理》相关节目：先结论后理由的表达结构'] },
        { title: '个人兴趣更新', items: ['AI 原生硬件与人机交互', '结构化表达', '个人知识管理'] },
        { title: '更贴合你的内容', items: ['《金字塔原理》：构建清晰汇报和写作逻辑', 'Acquired 相关 AI 硬件公司节目：理解硬件产品的长期价值'] },
      ],
    },
    {
      id: `${id}-growth`, sceneId: 'growth', label: '成长建议', timeLabel: '本次上传',
      title: '汇报时先说结论，会让决策更快',
      summary: '工作对话中出现了信息铺垫过长的情况，可以尝试“结论—理由—数据”结构。', meta: '2 个建议方向',
      detailSections: [
        { title: '场景', content: '产品方案评审中，你向可能具有决策权的参会者介绍第一期范围。' },
        { title: '问题', content: '前两分钟主要在补充背景，核心结论出现较晚，对方两次追问“所以第一期到底做什么”。' },
        { title: '建议', items: ['开头 20 秒先说范围结论', '再用三点说明选择理由', '会前用一句话写下希望获得的决策'] },
      ],
    },
    {
      id: `${id}-inspiration`, sceneId: 'inspiration', label: '闲聊灵感', timeLabel: '本次上传',
      title: '让音频成为一个持续理解你的记忆层',
      summary: '你在对话中反复关注“不打断用户”的产品价值，这可以继续发展成 Always-on 的核心叙事。', meta: '1 个灵感',
      detailSections: [
        { title: '发生时背景', content: '在讨论首版 Demo 时，大家正在比较“用户主动查询”和“系统主动发现”两种价值。' },
        { title: '对话梗概', content: '你提出好的 Always-on 产品不应该要求用户频繁操作，而应在后台理解上下文，在有价值时再出现。' },
        { title: '对话价值', content: '这不只是功能描述，还可以成为产品的核心交互原则和品牌叙事。' },
        { title: '建议推荐', items: ['将“不打断”写成三条可验证的产品原则', '为每种卡片定义“什么时候值得出现”'] },
      ],
    },
  ];
  return {
    id,
    providerId,
    promptVersions: Object.fromEntries(Object.entries(prompts).map(([key, value]) => [key, value.version ?? 1])),
    date: '今天 · 8 月 4 日',
    uploadedAt: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
    audio,
    transcript,
    cards,
  };
}
