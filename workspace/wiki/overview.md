This wiki tracks research on workspace.

## Key Findings

基于 [Datawhale《FDE案例100》节选](/wiki/sources/fde-case-100.md)（Datawhale FDE案例100.pdf）：

- **核心角色**：[FDE（前沿部署工程师）](/wiki/concepts/fde-forward-deployed-engineer.md) 是连接业务与技术、推动 AI 真正落地的关键角色，其使命是解决 [AI落地最后一公里](/wiki/concepts/ai-last-mile.md) 问题。
- **核心方法论**：[小切口进入](/wiki/concepts/small-entry-point.md) 建立信任 → 按 [需求痛点评判标准](/wiki/concepts/pain-point-criteria.md) 筛选场景 → 以 [高频问题即测试集](/wiki/concepts/high-frequency-questions-as-test-set.md) 验证价值 → 将 [AI嵌入业务流程](/wiki/concepts/ai-embedded-in-workflow.md) → 最终实现 [组织能力沉淀](/wiki/concepts/organizational-capability-building.md)。
- **量化效果**：车管所审核时间从 15 分钟降至 3-5 分钟；律师报告生成从 3-5 天降至 20 分钟。

基于 [数据平台-项目技术总结](/wiki/sources/eco-data-platform-tech-summary.md)（数据平台-项目技术总结.md）：

- **FDE 工程实践实例**：[生态环境数据平台](/wiki/entities/eco-environment-data-platform.md) 由一人独立完成全链路设计开发并交付客户离线内网（[离线内网交付](/wiki/concepts/offline-intranet-delivery.md)），是 [FDE（前沿部署工程师）](/wiki/concepts/fde-forward-deployed-engineer.md) 工作形态的典型工程案例。
- **AI 协作开发方法论**：[SDD规范驱动开发](/wiki/concepts/sdd-spec-driven-development.md)（[OpenSpec](/wiki/entities/openspec.md) + Superpowers）实现需求—设计—代码—测试全链路可追溯，配合 [探索留档](/wiki/concepts/research-archiving.md) 将隐性调研知识结构化。
- **可复用工程资产**：[采集可靠性体系](/wiki/concepts/collection-reliability-system.md)、[幂等设计](/wiki/concepts/idempotent-design.md)、[Open API安全体系](/wiki/concepts/open-api-security-system.md)。

## Cases（案例）

1. [德国斯图加特零部件制造企业](/wiki/entities/stuttgart-parts-manufacturer.md) —— [隐性经验结构化](/wiki/concepts/tacit-knowledge-structuralization.md)
2. [车管所（浙江）](/wiki/entities/zhejiang-vehicle-management-office.md) —— 机器视觉 + OCR + RPA 智能审核
3. [法索AI](/wiki/entities/fasuo-ai.md) —— [案件工作台](/wiki/concepts/case-workbench.md) + [人机分工协作架构](/wiki/concepts/human-machine-collaboration-architecture.md) + [交付价值原则](/wiki/concepts/deliverable-value-principle.md)
4. [规划设计院](/wiki/entities/urban-planning-design-institute.md) —— AI 调度 GIS 工具，关键在于找到 [执行一号位](/wiki/concepts/executive-owner.md)
5. [生态环境数据平台](/wiki/entities/eco-environment-data-platform.md) —— [SDD规范驱动开发](/wiki/concepts/sdd-spec-driven-development.md) + [离线内网交付](/wiki/concepts/offline-intranet-delivery.md) + [采集可靠性体系](/wiki/concepts/collection-reliability-system.md)

## Key Entities

- [Datawhale](/wiki/entities/datawhale.md) —— 《[FDE案例100](/wiki/entities/fde-case-100.md)》发起方
- [得到AI学习圈](/wiki/entities/dedao-ai-learning-circle.md) —— 案例呈现合作方
- [生态环境数据平台](/wiki/entities/eco-environment-data-platform.md) —— 生态监测一体化数据平台（6+ 数据源、54 表、13 个 Open API）
- [OpenSpec](/wiki/entities/openspec.md) —— SDD 规范驱动开发工具
- [Snail Job](/wiki/entities/snail-job.md) —— 分布式定时任务调度框架
- [RustFS](/wiki/entities/rustfs.md) —— S3 兼容对象存储
- [HJ212环保协议](/wiki/entities/hj212-protocol.md) —— 污染物在线监控数据传输标准

## Sources

- [Datawhale《FDE案例100》节选](/wiki/sources/fde-case-100.md)
- [数据平台-项目技术总结](/wiki/sources/eco-data-platform-tech-summary.md)