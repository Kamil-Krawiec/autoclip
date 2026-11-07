import { useEffect, useState } from 'react'
import { formatDuration, type Clip, type ClipStatus } from '../api'

interface ClipCardProps {
  clip: Clip
  busy: boolean
  onStatus: (status: ClipStatus) => void
  onEdit: () => void
  onExport: () => void
}

function Thumbnail({ clip }: { clip: Clip }) {
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    if (!failed || attempt >= 4) return
    // Lazy cards may race for the single CPU render slot. Retry a bounded
    // number of times, staggered, without leaving an unbounded render queue.
    const timer = window.setTimeout(() => {
      setFailed(false)
      setAttempt(value => value + 1)
    }, 600 * 2 ** attempt + Math.random() * 500)
    return () => window.clearTimeout(timer)
  }, [failed, attempt])
  const url = `/api/clips/${encodeURIComponent(clip.id)}/thumbnail?start=${clip.start_s}&end=${clip.end_s}&retry=${attempt}`
  return (
    <div className="relative flex aspect-video items-center justify-center bg-ink-800 text-center text-sm text-ink-300">
      {failed ? (
        <div className="px-4 py-2" role="status" aria-label="Clip thumbnail unavailable">
          <p>{attempt < 4 ? 'Loading preview…' : 'Preview unavailable · media missing, audio only, or renderer busy'}</p>
          <button type="button" className="mt-1 min-h-11 underline" onClick={() => { setFailed(false); setAttempt(0) }}>Retry preview</button>
        </div>
      ) : (
        <img src={url} alt={`Source preview for ${clip.title || `clip ${clip.rank}`}`} loading="lazy" decoding="async" width={320} height={180} className="absolute h-full w-full object-contain" onError={() => setFailed(true)} />
      )}
    </div>
  )
}

export function ClipCard({ clip, busy, onStatus, onEdit, onExport }: ClipCardProps) {
  const kept = clip.status === 'kept'
  const discarded = clip.status === 'discarded'

  return (
    <article className="overflow-hidden border border-ink-700 bg-ink-850">
      <Thumbnail key={`${clip.id}:${clip.start_s}:${clip.end_s}`} clip={clip} />

      <div className="space-y-4 p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="eyebrow">Clip {clip.rank}</p>
            <h2 className="mt-1 text-base font-medium leading-snug text-ink-100">
              {clip.title || 'Untitled clip'}
            </h2>
          </div>
          <span className="numeric shrink-0 font-display text-2xl leading-none text-sodium-500" aria-label={`Score ${clip.score}`}>
            {clip.score}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-ink-300">
          <span className="numeric">{formatDuration(clip.duration_s)}</span>
          <span className="capitalize">{clip.status}</span>
          {clip.exports.length > 0 && (
            <span className="text-signal-good">{clip.exports.length} export{clip.exports.length === 1 ? '' : 's'}</span>
          )}
        </div>

        {clip.exports.length > 0 && (
          <ul aria-label="Export history" className="space-y-1 text-sm">
            {clip.exports.map((item) => (
              <li key={item.id}>
                <a className="text-sodium-400 underline underline-offset-2 hover:text-sodium-400" href={item.download_url}>
                  Download export · {new Date(item.created_at).toLocaleDateString()}
                </a>
              </li>
            ))}
          </ul>
        )}

        <details>
          <summary className="min-h-11 cursor-pointer py-2 text-sm text-ink-200 focus-visible:outline-offset-2">
            Why this clip
          </summary>
          <p className="pb-2 text-sm leading-relaxed text-ink-300">{clip.reason}</p>
        </details>

        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn btn-ghost"
            disabled={busy}
            onClick={() => onStatus(kept ? 'candidate' : 'kept')}
          >
            {kept ? 'Undo keep' : 'Keep'}
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            disabled={busy}
            onClick={() => onStatus(discarded ? 'candidate' : 'discarded')}
          >
            {discarded ? 'Undo drop' : 'Drop'}
          </button>
          <button type="button" className="btn btn-ghost" disabled={busy} onClick={onEdit}>
            Edit
          </button>
          <button type="button" className="btn btn-primary" disabled={busy} onClick={onExport}>
            {clip.exports.length > 0 ? 'Export again' : 'Export'}
          </button>
        </div>
      </div>
    </article>
  )
}
