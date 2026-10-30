import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import type { CropPath, ManualFraming } from '../api'
import type { FramingGeometry, PanRequest } from './ClipPlayer'

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
  startS,
  framing,
  cropPath,
  disabled,
  onCommit,
  onDirtyChange,
  onDraftGeometry,
  panRequest,
}: {
  startS: number
  framing: ManualFraming
  cropPath: CropPath | null
  disabled: boolean
  onCommit: (framing: ManualFraming) => Promise<boolean>
  onDirtyChange: (dirty: boolean) => void
  onDraftGeometry: (geometry: FramingGeometry | null) => void
  panRequest: PanRequest | null
}) {
  const [draft, setDraft] = useState<ManualFraming>(framing)
  const draftRef = useRef<ManualFraming>(framing)
  const savedRef = useRef<ManualFraming>(framing)
  const lastPan = useRef(panRequest)
  const [committing, setCommitting] = useState(false)

  savedRef.current = framing
  draftRef.current = draft

  useEffect(() => {
    setDraft(framing)
    draftRef.current = framing
  }, [framing.center_x, framing.center_y, framing.zoom, startS])

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

  useEffect(() => {
    onDraftGeometry(geometry)
  }, [geometry, onDraftGeometry])

  useEffect(() => () => onDraftGeometry(null), [onDraftGeometry])

  useEffect(() => {
    if (panRequest === lastPan.current) return
    lastPan.current = panRequest
    if (!panRequest || disabled || !geometry) return
    if (panRequest.resize) {
      const delta = Math.abs(panRequest.dx) >= Math.abs(panRequest.dy) ? panRequest.dx : panRequest.dy
      const nextWidth = clamp(geometry.widthNorm + delta * 2, 0.05, 1)
      updateDraft((current) => ({ ...current, zoom: current.zoom * geometry.widthNorm / nextWidth }))
    } else {
      updateDraft((current) => ({
        ...current,
        center_x: current.center_x + panRequest.dx,
        center_y: current.center_y + panRequest.dy,
      }))
    }
  }, [panRequest])

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
            Drag the orange frame to move it. Use its handle to resize; arrow keys also work.
          </p>
        </div>
        {isDirty && (
          <span className="text-xs font-medium text-sodium-500" role="status">
            Unsaved framing
          </span>
        )}
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
