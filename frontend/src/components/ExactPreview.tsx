import { useCallback, useEffect, useRef, useState } from 'react'

import { api, formatTimecode, type Clip } from '../api'
import { ErrorNote } from './ErrorNote'

const PREVIEW_DURATION_S = 3

export function ExactPreview({
  clip,
  playheadSourceS,
  signature,
  disabled,
  disabledReason,
}: {
  clip: Clip
  playheadSourceS: number
  signature: string
  disabled: boolean
  disabledReason?: string | null
}) {
  const [rendering, setRendering] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [renderedRange, setRenderedRange] = useState<{
    clipId: string
    signature: string
    startOffsetS: number
    durationS: number
  } | null>(null)

  const requestSeqRef = useRef(0)
  const activeUrlRef = useRef<string | null>(null)
  const currentSignatureRef = useRef(signature)
  const currentClipIdRef = useRef(clip.id)

  currentSignatureRef.current = signature
  currentClipIdRef.current = clip.id

  const revokeCurrentUrl = useCallback(() => {
    if (activeUrlRef.current) {
      URL.revokeObjectURL(activeUrlRef.current)
      activeUrlRef.current = null
    }
    setPreviewUrl(null)
    setRenderedRange(null)
  }, [])

  // Clear any rendered preview or stale in-flight response whenever the clip or
  // its saved edit signature changes.
  useEffect(() => {
    requestSeqRef.current += 1
    setRendering(false)
    setError(null)
    revokeCurrentUrl()
  }, [clip.id, signature, revokeCurrentUrl])

  useEffect(() => {
    return () => {
      requestSeqRef.current += 1
      if (activeUrlRef.current) {
        URL.revokeObjectURL(activeUrlRef.current)
        activeUrlRef.current = null
      }
    }
  }, [])

  const clipDuration = Math.max(0.01, clip.end_s - clip.start_s)
  const rawOffset = Number.isFinite(playheadSourceS) ? playheadSourceS - clip.start_s : 0
  const clampedOffset = Math.max(0, Math.min(rawOffset, Math.max(0, clipDuration - 0.05)))
  const roundedOffset = Math.round(clampedOffset * 100) / 100
  const effectiveDuration = Math.max(
    0.05,
    Math.min(PREVIEW_DURATION_S, Math.round((clipDuration - roundedOffset) * 100) / 100),
  )

  const runPreview = async (startOffsetS: number) => {
    if (disabled || rendering) return
    const reqSeq = requestSeqRef.current + 1
    requestSeqRef.current = reqSeq
    const targetClipId = clip.id
    const targetSignature = signature
    const durationS = Math.max(
      0.05,
      Math.min(PREVIEW_DURATION_S, Math.round((clipDuration - startOffsetS) * 100) / 100),
    )

    setRendering(true)
    setError(null)

    try {
      const blob = await api.previewClip(targetClipId, {
        start_s: startOffsetS,
        duration_s: PREVIEW_DURATION_S,
      })
      if (
        requestSeqRef.current !== reqSeq ||
        currentClipIdRef.current !== targetClipId ||
        currentSignatureRef.current !== targetSignature
      ) {
        return
      }
      const nextUrl = URL.createObjectURL(blob)
      if (activeUrlRef.current) {
        URL.revokeObjectURL(activeUrlRef.current)
      }
      activeUrlRef.current = nextUrl
      setPreviewUrl(nextUrl)
      setRenderedRange({
        clipId: targetClipId,
        signature: targetSignature,
        startOffsetS,
        durationS,
      })
    } catch (err) {
      if (
        requestSeqRef.current !== reqSeq ||
        currentClipIdRef.current !== targetClipId ||
        currentSignatureRef.current !== targetSignature
      ) {
        return
      }
      setError(err as Error)
    } finally {
      if (
        requestSeqRef.current === reqSeq &&
        currentClipIdRef.current === targetClipId &&
        currentSignatureRef.current === targetSignature
      ) {
        setRendering(false)
      }
    }
  }

  const lastStartOffsetS = renderedRange?.startOffsetS ?? roundedOffset

  return (
    <div className="space-y-3 border border-ink-800 bg-ink-900/40 p-3" aria-label="Exact preview">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <span className="eyebrow">Exact preview (FFmpeg / ASS)</span>
          <p className="mt-0.5 text-xs text-ink-400">
            Renders {effectiveDuration.toFixed(1)}s from playhead ({formatTimecode(roundedOffset)}) using saved settings.
          </p>
        </div>
        <button
          type="button"
          disabled={disabled || rendering}
          onClick={() => void runPreview(roundedOffset)}
          className="btn btn-primary"
        >
          {rendering ? 'Rendering preview…' : previewUrl ? 'Re-render exact preview' : 'Render exact preview'}
        </button>
      </div>

      {disabled && disabledReason && (
        <p className="text-xs text-sodium-500">{disabledReason}</p>
      )}

      {error && (
        <div className="space-y-2">
          <ErrorNote error={error} onDismiss={() => setError(null)} />
          <button
            type="button"
            disabled={disabled || rendering}
            onClick={() => void runPreview(lastStartOffsetS)}
            className="btn btn-ghost text-xs"
          >
            Retry exact preview
          </button>
        </div>
      )}

      {previewUrl && renderedRange && renderedRange.signature === signature && (
        <div className="space-y-2 pt-1">
          <div className="flex items-baseline justify-between text-xs text-ink-400">
            <span>
              Rendered clip offset {formatTimecode(renderedRange.startOffsetS)} –{' '}
              {formatTimecode(renderedRange.startOffsetS + renderedRange.durationS)}
            </span>
            <button
              type="button"
              onClick={revokeCurrentUrl}
              className="btn btn-quiet text-xs"
            >
              Clear preview
            </button>
          </div>
          <video
            key={previewUrl}
            src={previewUrl}
            controls
            playsInline
            className="mx-auto max-h-[48vh] w-full bg-ink-950 object-contain"
          />
        </div>
      )}
    </div>
  )
}
