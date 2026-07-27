import { Link } from 'react-router'
import { Panel } from '../components/Ui'

const PIPELINE = [
  ['01', '冻结数据', '按交易日与决策时点冻结供应商数据，生成不可变快照和 Manifest。'],
  ['02', '结构化研究', '基本面、技术面与事件情绪 Agent 只产出经过契约校验的子分和证据。'],
  ['03', '确定性评分', '版本化公式汇总评分、质量置信度与风险调整，不由模型直接决定最终分。'],
  ['04', '候选与组合', '通过可交易性、事件风险、容量和组合约束后形成正式候选与模拟组合。'],
  ['05', '报告与验证', '发布冻结报告，并用事件驱动回测和样本外指标验证策略表现。'],
]

const GUARDRAILS = [
  ['时点正确', '所有研究事实均可按 symbol、trading_date、available_at 与 decision_at 追溯，拒绝未来信息。'],
  ['数据可复现', '原始载荷、快照、模型产物和报告保留来源、采集时间、版本与 SHA-256。'],
  ['结论可解释', '正式结论来自冻结报告与确定性公式；最新行情和 K 线只用于观察，不改写历史结论。'],
  ['规则默认拒绝', '涨跌停、T+1、停复牌、费用等规则缺失或冲突时，不猜测放行交易。'],
]

const STACK = [
  ['研究与服务', 'Python · FastAPI · Pydantic · SQLAlchemy'],
  ['数据与检索', 'PostgreSQL · Parquet · DuckDB · MinIO/S3'],
  ['任务编排', 'Redis 队列 · Research / Backtest / Trade Plan Workers'],
  ['交互界面', 'React · TypeScript · Vite · Nginx'],
]

export function AboutPage() {
  return <div className="page-stack about-page">
    <section className="about-hero">
      <div className="about-mark" aria-hidden="true">霁</div>
      <div>
        <span className="eyebrow">ABOUT JIHENG RESEARCH</span>
        <h2>霁衡智研 · A 股 AI 自动投研系统</h2>
        <p>面向收盘后研究的可复现工作台，将数据冻结、结构化分析、确定性评分、模拟组合、个股判断与事件驱动回测连接成一条可审计链路。</p>
        <div className="about-actions">
          <Link className="primary" to="/research">发起每日研究</Link>
          <Link className="secondary" to="/reports">查看研究报告</Link>
        </div>
      </div>
      <aside>
        <span>系统定位</span>
        <strong>研究 · 回测 · 模拟</strong>
        <small>不连接自动实盘下单</small>
      </aside>
    </section>

    <Panel title="从数据到报告" eyebrow="RESEARCH PIPELINE">
      <div className="about-pipeline">
        {PIPELINE.map(([index, title, description]) => <article key={index}>
          <span>{index}</span>
          <div><strong>{title}</strong><p>{description}</p></div>
        </article>)}
      </div>
    </Panel>

    <div className="about-grid">
      <Panel title="系统守则" eyebrow="GUARDRAILS">
        <div className="about-principles">
          {GUARDRAILS.map(([title, description]) => <article key={title}>
            <i aria-hidden="true">✓</i>
            <div><strong>{title}</strong><p>{description}</p></div>
          </article>)}
        </div>
      </Panel>
      <Panel title="技术组成" eyebrow="PLATFORM">
        <div className="about-stack">
          {STACK.map(([title, detail]) => <div key={title}><span>{title}</span><strong>{detail}</strong></div>)}
        </div>
        <p className="about-footnote">分析只读取已提交的不可变 Manifest；控制面、数据湖与对象存储各自保留版本和审计信息。</p>
      </Panel>
    </div>

    <section className="about-boundary">
      <span aria-hidden="true">!</span>
      <div><strong>使用边界</strong><p>本系统输出仅用于研究、回测与模拟组合，不构成任何投资建议。市场行情可能延迟，历史表现不代表未来收益；正式决策应结合自身风险承受能力独立判断。</p></div>
    </section>
  </div>
}
