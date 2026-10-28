import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useBeforeUnload, useBlocker, useNavigate, useParams } from 'react-router-dom'

import { api, type Clip, type ClipStatus, type Job } from '../api'
import { ClipCard } from '../components/ClipCard'
import { ErrorNote } from '../components/ErrorNote'

export function ResultsPage() {
  const { jobId } = useParams()
  const navigate = useNavigate()
  const [job, setJob] = useState<Job | null>(null)
  const [clips, setClips] = useState<Clip[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [exportingKept, setExportingKept] = useState(false)
  const operationLock = useRef(false)
  const blocker = useBlocker(() => operationLock.current)
  useBeforeUnload((event) => {
    if (!operationLock.current) return
    event.preventDefault()
    event.returnValue = ''
  })

  const loadResults = useCallback(async (isCurrent: () => boolean = () => true) => {
    setLoading(true)
    setError(null)
    try {
      const [nextJob, nextClips] = await Promise.all([api.getJob(jobId!), api.listClips(jobId!)])
      if (isCurrent()) {
        setJob(nextJob)
        setClips(nextClips)
      }
    } catch (err) {
      if (isCurrent()) setError(err as Error)
    } finally {
      if (isCurrent()) setLoading(false)
    }
  }, [jobId])

  useEffect(() => {
    let cancelled = false
    void loadResults(() => !cancelled)
    return () => { cancelled = true }
  }, [loadResults])

  const setStatus = async (clip: Clip, status: ClipStatus) => {
    if (operationLock.current) return
    operationLock.current = true
    setBusyId(clip.id)
    setError(null)
    try {
      const updated = await api.patchClip(clip.id, { status })
      setClips((current) => current.map((item) => item.id === updated.id ? updated : item))
    } catch (err) {
      setError(err as Error)
    } finally {
      setBusyId(null)
      operationLock.current = false
    }
  }

  const exportOne = async (clip: Clip) => {
    if (operationLock.current) return
    operationLock.current = true
    setBusyId(clip.id)
    setError(null)
    try {
      await api.exportClip(clip.id)
      const updated = await api.getClip(clip.id)
      setClips((current) => current.map((item) => item.id === updated.id ? updated : item))
    } catch (err) {
      setError(err as Error)
    } finally {
      setBusyId(null)
      operationLock.current = false
    }
  }

  const exportAllKept = async () => {
    if (operationLock.current) return
    operationLock.current = true
    setExportingKept(true)
    setError(null)
    try {
      for (const clip of clips.filter((item) => item.status === 'kept')) {
        setBusyId(clip.id)
        await api.exportClip(clip.id)
        const updated = await api.getClip(clip.id)
        setClips((current) => current.map((item) => item.id === updated.id ? updated : item))
      }
    } catch (err) {
      setError(err as Error)
    } finally {
      setBusyId(null)
      setExportingKept(false)
      operationLock.current = false
    }
  }

  if (loading) return <p className="pt-24 text-sm text-ink-300" role="status">Loading results…</p>
  if (error && !job) return (
    <div className="max-w-3xl pt-16">
      <ErrorNote error={error} />
      <button type="button" className="btn btn-ghost mt-5" onClick={() => void loadResults()}>Retry loading results</button>
    </div>
  )
  if (!job) return null

  const keptCount = clips.filter((clip) => clip.status === 'kept').length
  return (
    <section className="pt-10">
      <div className="flex flex-wrap items-end justify-between gap-4 border-b border-ink-800 pb-5">
        <div className="min-w-0">
          <Link to="/" className="eyebrow hover:text-sodium-400">← All jobs</Link>
          <h1 className="mt-2 truncate font-display text-3xl text-ink-100">{job.source?.title || 'Untitled'}</h1>
          <p className="mt-2 text-sm text-ink-300">{clips.length} clips · {keptCount} kept · {job.status}</p>
        </div>
        <button className="btn btn-primary" disabled={keptCount === 0 || Boolean(busyId) || exportingKept} onClick={exportAllKept}>
          {exportingKept ? 'Rendering…' : `Export kept (${keptCount})`}
        </button>
      </div>

      {error && <div className="mt-5 max-w-3xl"><ErrorNote error={error} onDismiss={() => setError(null)} /></div>}
      {blocker.state === 'blocked' && (
        <div role="alert" className="mt-4 flex flex-wrap items-center gap-3 border-l-2 border-sodium-500 bg-ink-850 p-3 text-sm text-ink-100">
          <span>{operationLock.current ? 'Wait for the current operation to finish before leaving results.' : 'Leave results?'}</span>
          <button className="btn btn-ghost" onClick={() => blocker.reset()}>Stay</button>
          {!operationLock.current && <button className="btn btn-primary" onClick={() => blocker.proceed()}>Leave</button>}
        </div>
      )}

      {clips.length === 0 ? (
        error ? (
          <button
            type="button"
            className="btn btn-ghost mt-6"
            onClick={() => void loadResults()}
          >
            Retry loading results
          </button>
        ) : <p className="pt-16 text-sm text-ink-300">No clips are available for this job.</p>
      ) : (
        <div className="mt-8 grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-3">
          {clips.map((clip) => (
            <ClipCard
              key={clip.id}
              clip={clip}
              busy={Boolean(busyId) || exportingKept}
              onStatus={(status) => void setStatus(clip, status)}
              onEdit={() => navigate(`/jobs/${job.id}/clips/${clip.id}`)}
              onExport={() => void exportOne(clip)}
            />
          ))}
        </div>
      )}
    </section>
  )
}
