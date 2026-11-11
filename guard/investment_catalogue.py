"""Fixed AI Berkshire research entry points adapted to the group bot."""

SOURCE_REPOSITORY = "https://github.com/xbtlin/ai-berkshire"
SOURCE_COMMIT = "ab758d78803d01ce585bf3dd1e241e23330116cd"
SOURCE_LICENSE = "MIT"

ENTRYPOINTS = (
    {"id": "investment-research", "name": "综合投研", "focus": "业务本质、护城河、逆向风险、管理层、趋势、估值和安全边际", "terms": ("全面研究", "深度研究", "综合分析", "投资研究")},
    {"id": "company-valuation", "name": "公司估值", "focus": "现金流、可比估值、反向估值、情景区间和可复算性", "terms": ("估值", "DCF", "内在价值", "合理价格", "安全边际")},
    {"id": "investment-team", "name": "多视角投研", "focus": "商业、财务、行业、风险与长期确定性的相互质疑", "terms": ("四大师", "多视角", "投资团队", "对抗分析")},
    {"id": "management-deep-dive", "name": "管理层研究", "focus": "资本配置、治理、激励、诚信与关键人依赖", "terms": ("管理层", "治理", "资本配置", "创始人")},
    {"id": "private-company-research", "name": "非上市公司研究", "focus": "仅基于可核验公开披露；非上市公司财务与估值覆盖通常不足", "terms": ("非上市", "未上市", "私营公司", "私人公司")},
    {"id": "deep-company-series", "name": "公司系列研究", "focus": "将长篇系列研究压缩为单次深度摘要；不生成或保存多篇报告", "terms": ("系列研究", "长篇研究", "公司系列")},
    {"id": "earnings-review", "name": "财报精读", "focus": "优先原始年报、季报、业绩公告及报告期口径", "terms": ("财报", "年报", "季报", "业绩公告", "财报精读")},
    {"id": "earnings-team", "name": "财报多视角分析", "focus": "多视角解读财报并综合；只在群内答复，不发布文章", "terms": ("财报团队", "财报多视角", "财报对抗")},
    {"id": "industry-research", "name": "行业研究", "focus": "产业链、利润池、竞争格局、资本强度和价值捕获", "terms": ("行业研究", "产业链", "行业全景", "竞争格局")},
    {"id": "industry-funnel", "name": "行业漏斗筛选", "focus": "只筛选问题中明确给出的行业或公司；公开样本不足时标为部分覆盖", "terms": ("行业筛选", "漏斗", "筛选公司", "选股")},
    {"id": "quality-screen", "name": "质量筛选", "focus": "按业务质量、财务韧性、治理和证据完整度筛除弱项", "terms": ("质量筛选", "硬指标", "去劣", "质量排名")},
    {"id": "bottleneck-hunter", "name": "供应链瓶颈研究", "focus": "定位产业链物理瓶颈、议价权、替代性和供给约束", "terms": ("瓶颈", "供应链", "供给约束", "产业链卡点")},
    {"id": "era-alpha", "name": "长期趋势研究", "focus": "区分宏观趋势与公司价值捕获，检验增长持续性和退出条件", "terms": ("时代趋势", "长期趋势", "时代alpha", "成长主线")},
    {"id": "investment-checklist", "name": "买入前清单", "focus": "能力圈、生意、护城河、管理层、安全边际和决策纪律", "terms": ("买入清单", "买入前", "投资清单", "镜子测试")},
    {"id": "income-investment", "name": "股息与收益研究", "focus": "分红来源、覆盖率、资产负债表、可持续性和收益陷阱", "terms": ("股息", "股息率", "分红", "派息", "收益率")},
    {"id": "portfolio-review", "name": "个人组合分析", "focus": "只使用提问者本人当前明确提供的信息，不读取他人数据或另存档案", "terms": ("组合分析", "组合优化", "仓位", "持仓", "再平衡")},
    {"id": "thesis-tracker", "name": "投资逻辑跟踪", "focus": "仅依据当前问题和提问者本人相关群历史；不建立长期投资档案", "terms": ("跟踪投资逻辑", "投资逻辑跟踪", "论文跟踪", "跟踪持仓")},
    {"id": "thesis-drift", "name": "投资逻辑变化比较", "focus": "比较本次提供或本人历史中的公开论据；缺少原始材料时明确受限", "terms": ("逻辑漂移", "论点变化", "对比报告", "两份报告")},
    {"id": "news-pulse", "name": "股价异动归因", "focus": "公司事件、监管、行业、市场情绪，区分事实与推测", "terms": ("异动", "大涨", "大跌", "为什么涨", "为什么跌", "新闻归因")},
    {"id": "dyp-ask", "name": "商业与投资思考", "focus": "以生意本质、能力圈、长期竞争力和决策纪律组织分析", "terms": ("段永平", "生意本质", "能力圈", "长期确定性")},
    {"id": "financial-data", "name": "财务数据核验", "focus": "保留报告期、单位和币种，关键数字优先双来源独立交叉核验", "terms": ("核对财务数据", "交叉验证", "财务数据", "数据核验")},
    {"id": "wechat-article", "name": "研究文章写作", "focus": "依据已核实的研究结论改写群内答复；不保存文件或发布公众号", "terms": ("写文章", "公众号", "文章", "读者评审")},
)
