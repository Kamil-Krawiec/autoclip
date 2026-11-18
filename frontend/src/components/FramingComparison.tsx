import { useEffect, useRef, useState } from 'react'
import { api, formatTimecode, type Clip, type SingleFraming } from '../api'
import { ErrorNote } from './ErrorNote'

export function FramingComparison({ clip, playheadSourceS, signature, disabled }: {
  clip: Clip
  playheadSourceS: number
  signature: string
  disabled: boolean
}) {
  const [images, setImages] = useState<{ label: string; url: string; region: string; collision: string }[]>([])
  const [time, setTime] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const requestId = useRef(0)

  useEffect(() => {
    requestId.current += 1
    setImages([])
    setLoading(false)
    setError(null)
    return () => { requestId.current += 1 }
  }, [clip.id, signature])

  async function compare() {
    const id = ++requestId.current
    const timestamp = Math.max(clip.start_s, Math.min(playheadSourceS, clip.end_s))
    const manual: SingleFraming = clip.framing?.mode === 'manual'
      ? clip.framing : { mode: 'manual', center_x: 0.5, center_y: 0.5, zoom: 1 }
    const modes: [string, SingleFraming][] = [
      ['Auto', { mode: 'auto' }], ['Fit', { mode: 'fit' }],
      ['Manual', manual], ['Split', { mode: 'split' }],
    ]
    setLoading(true)
    setImages([])
    setError(null)
    try {
      const results = []
      let revision: string | null = null
      for (const [label, framing] of modes) {
        const result = await api.compareFrame(clip.id, timestamp, framing)
        if (id !== requestId.current) return
        if (revision !== null && result.current_revision !== revision) {
          throw new Error('Saved settings changed during comparison. Please retry.')
        }
        revision = result.current_revision
        const image = result.frames[0]?.composed_image
        if (!image) throw new Error(`No frame returned for ${label}.`)
        const frame = result.frames[0]
        const region = frame.source_region.status === 'known' && frame.source_region.clipped_fraction !== undefined
          ? `${(frame.source_region.clipped_fraction * 100).toFixed(1)}% estimated source area outside foreground crop`
          : `Source clipping unknown: ${frame.source_region.reason ?? 'geometry unavailable'}`
        results.push({ label, url: `data:${image.mime_type};base64,${image.data_base64}`, region,
          collision: `Face/caption collision unknown: ${frame.face_caption_collision.reason ?? 'evidence unavailable'}` })
      }
      setTime(timestamp)
      setImages(results)
    } catch (err) {
      if (id === requestId.current) setError(err as Error)
    } finally {
      if (id === requestId.current) setLoading(false)
    }
  }

  return (
    <section className="space-y-3 border border-ink-800 bg-ink-900/40 p-3" aria-label="Framing comparison">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <span className="eyebrow">Compare framing</span>
          <p className="mt-0.5 text-xs text-ink-400">Compare saved settings at the playhead without changing the clip. Manual uses saved whole-clip settings or a centered default; Split uses default panels.</p>
        </div>
        <button type="button" className="btn btn-primary" disabled={disabled || loading} onClick={() => void compare()}>
          {loading ? 'Rendering comparison…' : 'Compare modes'}
        </button>
      </div>
      {error && <ErrorNote error={error} onDismiss={() => setError(null)} />}
      {images.length > 0 && <>
        <p className="text-xs text-ink-400">Source time {formatTimecode(time)}</p>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {images.map(({ label, url, region, collision }) => <figure key={label}>
            <img src={url} alt={`${label} framing at ${formatTimecode(time)}`} className="w-full" />
            <figcaption className="mt-1 text-sm text-ink-200">{label}<p className="text-xs text-ink-400">{region}</p><p className="text-xs text-ink-400">{collision}</p></figcaption>
          </figure>)}
        </div>
      </>}
    </section>
  )
}
