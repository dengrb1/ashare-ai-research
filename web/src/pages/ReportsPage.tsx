import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Report } from '../types'
import { Empty, ErrorNotice, formatTime, Loading, Panel, today } from '../components/Ui'

export function ReportsPage() {
  const [date, setDate] = useState(today())
  const [report, setReport] = useState<Report | null>(null)
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => {
    setLoading(true); setError(''); setContent('')
    api.report(date).then(async (row) => {
      setReport(row)
      if (row.content || row.body) setContent(row.content || row.body || '')
      else if (row.report_id) {
        try { const detail = await api.reportContent(row.report_id); setContent(detail.content || detail.body || '') } catch { setContent('报告正文存储于对象存储，可通过审计记录核验内容哈希。') }
      }
    }).catch((reason) => { setReport(null); setError(reason instanceof Error ? reason.message : '报告加载失败') }).finally(() => setLoading(false))
  }, [date])
  return <div className="report-layout">
    <Panel title="日报索引" eyebrow="RESEARCH REPORT" action={<label className="date-filter">交易日<input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label>}>
      {report ? <div className="report-index"><div><span>报告类型</span><strong>{report.report_type}</strong></div><div><span>报告 ID</span><code>{report.report_id}</code></div><div><span>关联运行</span><code>{report.run_id}</code></div><div><span>生成时间</span><strong>{formatTime(report.created_at)}</strong></div><div><span>对象地址</span><small>{report.object_uri || 'API 正文'}</small></div></div> : !loading && <Empty title="暂无报告索引" />}
    </Panel>
    <Panel title={`${date} A 股每日研究报告`} eyebrow="DAILY BRIEF" className="report-paper">
      <ErrorNotice message={error} />
      {loading ? <Loading /> : report ? <article>{content.split('\n').map((line, index) => line.startsWith('#') ? <h3 key={index}>{line.replace(/^#+\s*/, '')}</h3> : line.trim() ? <p key={index}>{line}</p> : <br key={index} />)}{!content && <p>报告已生成，正文可从对象存储读取。</p>}</article> : <Empty title="该交易日暂无报告" description="日报会在每日研究流水线最后阶段生成" />}
    </Panel>
  </div>
}
