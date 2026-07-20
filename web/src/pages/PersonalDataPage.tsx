import { useEffect, useState } from 'react'
import { api, downloadPersonalArchive } from '../api'
import { ErrorNotice, formatTime, Panel } from '../components/Ui'
import type { PersonalArchiveJob } from '../types'

function useArchivePolling(job: PersonalArchiveJob | null, load: (id: string) => Promise<PersonalArchiveJob>, setJob: (job: PersonalArchiveJob) => void) {
  useEffect(() => {
    if (!job || !['PENDING', 'PROCESSING'].includes(job.status)) return
    const timer = window.setInterval(() => void load(job.archive_id).then(setJob), 1500)
    return () => window.clearInterval(timer)
  }, [job?.archive_id, job?.status])
}

export function PersonalDataPage() {
  const [exportPassphrase, setExportPassphrase] = useState('')
  const [importPassphrase, setImportPassphrase] = useState('')
  const [importFile, setImportFile] = useState<File | null>(null)
  const [exportJob, setExportJob] = useState<PersonalArchiveJob | null>(null)
  const [importJob, setImportJob] = useState<PersonalArchiveJob | null>(null)
  const [applyJob, setApplyJob] = useState<PersonalArchiveJob | null>(null)
  const [mergeOptions, setMergeOptions] = useState<Record<string, unknown>>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useArchivePolling(exportJob, api.personalExport, setExportJob)
  useArchivePolling(importJob, api.personalImport, setImportJob)
  useArchivePolling(applyJob, api.personalImport, setApplyJob)

  async function startExport() {
    if (exportPassphrase.length < 8) { setError('一次性口令至少需要 8 个字符'); return }
    setBusy(true); setError('')
    try { setExportJob(await api.createPersonalExport(exportPassphrase)); setExportPassphrase('') }
    catch (reason) { setError(reason instanceof Error ? reason.message : '个人档案导出提交失败') }
    finally { setBusy(false) }
  }

  async function download() {
    if (!exportJob) return
    setBusy(true); setError('')
    try {
      const blob = await downloadPersonalArchive(exportJob.archive_id)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a'); link.href = url; link.download = 'personal-profile.ashare'; link.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '个人档案下载失败') }
    finally { setBusy(false) }
  }

  async function startImport() {
    if (!importFile) { setError('请选择个人档案文件'); return }
    if (importPassphrase.length < 8) { setError('请输入导出时设置的一次性口令'); return }
    setBusy(true); setError(''); setApplyJob(null); setMergeOptions({})
    try { setImportJob(await api.uploadPersonalImport(importFile, importPassphrase)); setImportPassphrase('') }
    catch (reason) { setError(reason instanceof Error ? reason.message : '个人档案上传失败') }
    finally { setBusy(false) }
  }

  async function applyImport() {
    if (!importJob) return
    setBusy(true); setError('')
    try { setApplyJob(await api.applyPersonalImport(importJob.archive_id, mergeOptions, crypto.randomUUID())) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '个人档案合并提交失败') }
    finally { setBusy(false) }
  }

  const preview = (importJob?.result || {}) as {
    watchlist?: { new?: string[]; duplicate?: string[] }
    positions?: { new?: string[]; conflicts?: Array<{ symbol: string; current: unknown; imported: unknown }> }
    total_assets?: { current?: string | null; imported?: string | null }
    research_preference?: { current?: unknown; imported?: unknown }
    history?: Record<string, number>
  }
  const conflicts = preview.positions?.conflicts || []

  return <div className="page-stack personal-data-page">
    <div className="archive-warning"><strong>图片不会进入个人档案</strong><span>聊天图片、缩略图和模型分析副本均不导出；档案中只保留“图片未包含在个人档案中”占位。请自行保存原图。</span></div>
    <ErrorNotice message={error} />
    <div className="split-grid">
      <Panel title="导出完整个人档案" eyebrow="ENCRYPTED EXPORT">
        <p className="form-hint">导出包使用一次性口令加密，包含当前用户的持仓、自选、研究、报告、回测和文字对话，不包含账户凭据、会话、缓存或任何图片。生成文件保留 24 小时。</p>
        <label>一次性口令<input type="password" autoComplete="new-password" minLength={8} value={exportPassphrase} onChange={(event) => setExportPassphrase(event.target.value)} placeholder="至少 8 个字符；系统不保存明文" /></label>
        <button className="primary" disabled={busy || exportPassphrase.length < 8} onClick={() => void startExport()}>生成加密档案</button>
        {exportJob && <div className="archive-job"><strong>{exportJob.status === 'SUCCEEDED' ? '档案已就绪' : `正在处理：${exportJob.phase}`}</strong><span>进度 {exportJob.progress}% · 到期 {formatTime(exportJob.expires_at)}</span><progress max={100} value={exportJob.progress} />{exportJob.status === 'SUCCEEDED' && <div className="row-actions"><button className="primary" disabled={busy} onClick={() => void download()}>下载档案</button><button className="danger-button" onClick={() => void api.deletePersonalExport(exportJob.archive_id).then(() => setExportJob(null))}>立即删除</button></div>}{exportJob.status === 'FAILED' && <small>错误码：{exportJob.error_code || 'ARCHIVE_FAILED'}</small>}</div>}
      </Panel>
      <Panel title="导入并预览" eyebrow="VALIDATE BEFORE MERGE">
        <p className="form-hint">先验证口令、认证标签、逐文件哈希和安全路径，再生成新增、重复和冲突预览；确认前不会改变现有数据。</p>
        <label>个人档案文件<input type="file" accept=".ashare,application/vnd.ashare.personal-profile" onChange={(event) => setImportFile(event.target.files?.[0] || null)} /></label>
        <label>一次性口令<input type="password" autoComplete="current-password" minLength={8} value={importPassphrase} onChange={(event) => setImportPassphrase(event.target.value)} /></label>
        <button className="primary" disabled={busy || !importFile || importPassphrase.length < 8} onClick={() => void startImport()}>上传并生成预览</button>
        {importJob && <div className="archive-job"><strong>{importJob.status === 'SUCCEEDED' ? '预览已生成' : `正在处理：${importJob.phase}`}</strong><span>进度 {importJob.progress}%</span><progress max={100} value={importJob.progress} />{importJob.status === 'FAILED' && <small>错误码：{importJob.error_code || 'IMPORT_FAILED'}</small>}</div>}
      </Panel>
    </div>
    {importJob?.status === 'SUCCEEDED' && <Panel title="分类合并预览" eyebrow="CONFLICT REVIEW">
      <div className="archive-preview-grid">
        <section><strong>自选股并集</strong><p>新增 {(preview.watchlist?.new || []).length} 项，重复 {(preview.watchlist?.duplicate || []).length} 项；保留当前排序，新增项追加。</p></section>
        <section><strong>历史记录</strong><p>{Object.entries(preview.history || {}).map(([key, value]) => `${key} ${value}`).join(' · ') || '无历史记录'}</p></section>
        <section><strong>总资产</strong><p>当前：{preview.total_assets?.current ?? '未设置'} · 导入：{preview.total_assets?.imported ?? '未设置'}</p><select value={String(mergeOptions.total_assets || 'CURRENT')} onChange={(event) => setMergeOptions((current) => ({ ...current, total_assets: event.target.value }))}><option value="CURRENT">保留当前值</option><option value="IMPORTED">使用导入值</option></select></section>
        <section><strong>研究设置</strong><p>默认保留当前设置。</p><select value={String(mergeOptions.research_preference || 'CURRENT')} onChange={(event) => setMergeOptions((current) => ({ ...current, research_preference: event.target.value }))}><option value="CURRENT">保留当前值</option><option value="IMPORTED">使用导入值</option></select></section>
      </div>
      {conflicts.map((conflict) => <div className="archive-conflict" key={conflict.symbol}><strong>{conflict.symbol} 持仓冲突</strong><pre>当前：{JSON.stringify(conflict.current)}{`\n`}导入：{JSON.stringify(conflict.imported)}</pre><select value={String((mergeOptions.positions as Record<string, string> | undefined)?.[conflict.symbol] || 'CURRENT')} onChange={(event) => setMergeOptions((current) => ({ ...current, positions: { ...((current.positions as Record<string, string> | undefined) || {}), [conflict.symbol]: event.target.value } }))}><option value="CURRENT">保留当前持仓</option><option value="IMPORTED">使用导入持仓</option></select></div>)}
      <button className="primary" disabled={busy || Boolean(applyJob && ['PENDING', 'PROCESSING'].includes(applyJob.status))} onClick={() => void applyImport()}>确认并异步应用合并</button>
      {applyJob && <div className="archive-job"><strong>{applyJob.status === 'SUCCEEDED' ? '合并完成' : `合并状态：${applyJob.phase}`}</strong><span>进度 {applyJob.progress}%</span><progress max={100} value={applyJob.progress} />{applyJob.result && <pre>{JSON.stringify(applyJob.result, null, 2)}</pre>}</div>}
    </Panel>}
  </div>
}
