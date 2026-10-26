import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import type { CropPath, ManualFraming } from '../api'

const MIN_ZOOM = 1
const MAX_ZOOM = 4
const EPSILON = 1e-4

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min
  return Math.min(max, Math.max(min, value))
}

function sameFraming(a: ManualFraming, b: ManualFraming): boolean {
  return (
    Math.abs(a.center_x - b.center_x) <= EPSILON &&
    Math.abs(a.center_y - b.center_y) <= EPSILON &&
    Math.abs(a.zoom - b.zoom) <= EPSILON
  )
}

export function FramingEditor({
  src,
  startS,
  framing,
  cropPath,
  disabled,
  onCommit,
  onDirtyChange,
}: {
  src: string
  startS: number
  framing: ManualFraming
  cropPath: CropPath | null
  disabled: boolean
  onCommit: (framing: ManualFraming) => Promise<boolean>
  onDirtyChange: (dirty: boolean) => void
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const stageRef = useRef<HTMLDivElement>(null)
  const [draft, setDraft] = useState<ManualFraming>(framing)
  const draftRef = useRef<ManualFraming>(framing)
  const savedRef = useRef<ManualFraming>(framing)
  const [dragging, setDragging] = useState(false)
  const [committing, setCommitting] = useState(false)
  const activePointerIdRef = useRef<number | null>(null)

  savedRef.current = framing
  draftRef.current = draft

  useEffect(() => {
    setDraft(framing)
    draftRef.current = framing
    setDragging(false)
    activePointerIdRef.current = null
  }, [framing.center_x, framing.center_y, framing.zoom, startS])

  useEffect(() => {
    if (disabled) {
      setDragging(false)
      activePointerIdRef.current = null
    }
  }, [disabled])

  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    const seekToStart = () => {
      try {
        video.pause()
        if (Math.abs(video.currentTime - startS) > 0.05) {
          video.currentTime = Math.max(0, startS)
        }
      } catch {
        /* Ignore seek errors before metadata is ready. */
      }
    }
    seekToStart()
    video.addEventListener('loadedmetadata', seekToStart)
    return () => {
      video.removeEventListener('loadedmetadata', seekToStart)
    }
  }, [src, startS])

  // Draft bounds approximate the acknowledged server crop; FFmpeg parity stays server-owned.
  const bounds = useCallback((zoom: number) => {
    const sourceWidth = cropPath?.source_width ?? 0
    const sourceHeight = cropPath?.source_height ?? 0
    const segment = cropPath?.segments[0]
    if (!sourceWidth || !sourceHeight || !segment) return null
    const scale = framing.zoom / zoom
    const widthNorm = clamp(segment.width * scale / sourceWidth, 0, 1)
    const heightNorm = clamp(segment.height * scale / sourceHeight, 0, 1)
    return {
      sourceWidth, sourceHeight, widthNorm, heightNorm,
      minCenterX: widthNorm / 2, maxCenterX: 1 - widthNorm / 2,
      minCenterY: heightNorm / 2, maxCenterY: 1 - heightNorm / 2,
    }
  }, [cropPath, framing.zoom])

  const geometry = useMemo(() => {
    const size = bounds(draft.zoom)
    if (!size) return null
    return {
      ...size,
      leftNorm: clamp(draft.center_x, size.minCenterX, size.maxCenterX) - size.widthNorm / 2,
      topNorm: clamp(draft.center_y, size.minCenterY, size.maxCenterY) - size.heightNorm / 2,
    }
  }, [bounds, draft])

  const clampDraft = useCallback((candidate: ManualFraming): ManualFraming => {
    const zoom = clamp(Math.round(candidate.zoom * 100) / 100, MIN_ZOOM, MAX_ZOOM)
    const size = bounds(zoom)
    return {
      mode: 'manual', zoom,
      center_x: clamp(candidate.center_x, size?.minCenterX ?? 0, size?.maxCenterX ?? 1),
      center_y: clamp(candidate.center_y, size?.minCenterY ?? 0, size?.maxCenterY ?? 1),
    }
  }, [bounds])

  const updateDraft = useCallback(
    (updater: (current: ManualFraming) => ManualFraming) => {
      const next = clampDraft(updater(draftRef.current))
      draftRef.current = next
      setDraft(next)
      return next
    },
    [clampDraft],
  )

  const commitDraft = useCallback(
    async (candidate: ManualFraming) => {
      const normalized = clampDraft(candidate)
      draftRef.current = normalized
      setDraft(normalized)
      if (sameFraming(normalized, savedRef.current)) {
        return
      }
      setCommitting(true)
      try {
        const ok = await onCommit(normalized)
        if (!ok) {
          draftRef.current = savedRef.current
          setDraft(savedRef.current)
        }
      } catch {
        draftRef.current = savedRef.current
        setDraft(savedRef.current)
      } finally {
        setCommitting(false)
      }
    },
    [clampDraft, onCommit],
  )

  const moveCenterFromPointer = useCallback(
    (clientX: number, clientY: number) => {
      const rect = stageRef.current?.getBoundingClientRect()
      if (!rect || rect.width <= 0 || rect.height <= 0) return
      const rawX = (clientX - rect.left) / rect.width
      const rawY = (clientY - rect.top) / rect.height
      updateDraft((current) => ({
        ...current,
        center_x: rawX,
        center_y: rawY,
      }))
    },
    [updateDraft],
  )

  const locked = disabled || committing || !geometry
  const isDirty = !sameFraming(draft, framing)
  useEffect(() => { onDirtyChange(isDirty) }, [isDirty, onDirtyChange])

  return (
    <fieldset
      disabled={locked}
      className="space-y-3 border border-ink-800 bg-ink-900/40 p-3"
      aria-label="Manual framing editor"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <span className="eyebrow">Manual framing</span>
          <p className="mt-0.5 text-xs text-ink-400">
            Approximate draft below; the top player shows the last saved crop. Apply to update it.
          </p>
        </div>
        {isDirty && (
          <span className="text-xs font-medium text-sodium-500" role="status">
            Unsaved framing
          </span>
        )}
      </div>

      <div
        ref={stageRef}
        onPointerDown={(e) => {
          if (locked) return
          e.preventDefault()
          try {
            e.currentTarget.setPointerCapture(e.pointerId)
          } catch {
            /* Fallback when pointer capture is unavailable in synthetic tests. */
          }
          activePointerIdRef.current = e.pointerId
          setDragging(true)
          moveCenterFromPointer(e.clientX, e.clientY)
        }}
        onPointerMove={(e) => {
          if (!dragging || locked) return
          if (
            activePointerIdRef.current !== null &&
            e.pointerId !== activePointerIdRef.current
          ) {
            return
          }
          moveCenterFromPointer(e.clientX, e.clientY)
        }}
        onPointerUp={(e) => {
          if (!dragging) return
          if (
            activePointerIdRef.current !== null &&
            e.pointerId !== activePointerIdRef.current
          ) {
            return
          }
          try {
            if (e.currentTarget.hasPointerCapture(e.pointerId)) {
              e.currentTarget.releasePointerCapture(e.pointerId)
            }
          } catch {
            /* Ignore capture release errors. */
          }
          activePointerIdRef.current = null
          setDragging(false)
          void commitDraft(draftRef.current)
        }}
        onPointerCancel={() => {
          if (!dragging) return
          activePointerIdRef.current = null
          setDragging(false)
          draftRef.current = savedRef.current
          setDraft(savedRef.current)
        }}
        style={
          geometry
            ? { aspectRatio: `${geometry.sourceWidth} / ${geometry.sourceHeight}` }
            : undefined
        }
        className={[
          'relative w-full touch-none select-none overflow-hidden bg-ink-950',
          locked ? 'cursor-not-allowed opacity-70' : 'cursor-move',
        ].join(' ')}
      >
        <video
          ref={videoRef}
          src={src}
          muted
          playsInline
          preload="metadata"
          className="pointer-events-none h-full w-full object-contain"
        />

        {geometry && (
          <div
            data-testid="framing-crop-overlay"
            style={{
              left: `${geometry.leftNorm * 100}%`,
              top: `${geometry.topNorm * 100}%`,
              width: `${geometry.widthNorm * 100}%`,
              height: `${geometry.heightNorm * 100}%`,
            }}
            className="pointer-events-none absolute border-2 border-sodium-500 bg-sodium-500/10 shadow-[0_0_0_9999px_rgba(0,0,0,0.55)]"
          />
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <label className="block text-xs text-ink-300">
          <span className="flex justify-between">
            <span>Center X</span>
            <span className="numeric text-ink-400">{Math.round(draft.center_x * 100)}%</span>
          </span>
          <input
            type="range"
            aria-label="Center X"
            min={0}
            max={100}
            step={1}
            value={Math.round(draft.center_x * 100)}
            onChange={(e) => {
              const pct = Number(e.target.value)
              updateDraft((current) => ({ ...current, center_x: pct / 100 }))
            }}
            className="mt-1 w-full accent-sodium-500"
          />
        </label>

        <label className="block text-xs text-ink-300">
          <span className="flex justify-between">
            <span>Center Y</span>
            <span className="numeric text-ink-400">{Math.round(draft.center_y * 100)}%</span>
          </span>
          <input
            type="range"
            aria-label="Center Y"
            min={0}
            max={100}
            step={1}
            value={Math.round(draft.center_y * 100)}
            onChange={(e) => {
              const pct = Number(e.target.value)
              updateDraft((current) => ({ ...current, center_y: pct / 100 }))
            }}
            className="mt-1 w-full accent-sodium-500"
          />
        </label>

        <label className="block text-xs text-ink-300">
          <span className="flex justify-between">
            <span>Zoom</span>
            <span className="numeric text-ink-400">{draft.zoom.toFixed(1)}×</span>
          </span>
          <input
            type="range"
            aria-label="Zoom"
            min={MIN_ZOOM}
            max={MAX_ZOOM}
            step={0.1}
            value={draft.zoom}
            onChange={(e) => {
              const zoom = Number(e.target.value)
              updateDraft((current) => ({ ...current, zoom }))
            }}
            className="mt-1 w-full accent-sodium-500"
          />
        </label>
      </div>

      <div className="flex items-center justify-end gap-2 pt-1">
        <button
          type="button"
          disabled={locked || !isDirty}
          onClick={() => { draftRef.current = framing; setDraft(framing) }}
          className="btn btn-ghost"
        >
          Discard framing changes
        </button>
        <button
          type="button"
          disabled={locked || !isDirty}
          onClick={() => void commitDraft(draftRef.current)}
          className="btn btn-primary"
        >
          {committing ? 'Applying…' : 'Apply framing'}
        </button>
      </div>
    </fieldset>
  )
}
