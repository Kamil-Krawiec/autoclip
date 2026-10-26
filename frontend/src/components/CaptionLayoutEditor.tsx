import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import type { CaptionOverrides, CaptionStyle } from '../api'

const MIN_SIZE = 0.02
const MAX_SIZE = 0.10
const MIN_X = 0.10
const MAX_X = 0.90
const MIN_Y = 0.10
const MAX_Y = 0.95
const EPSILON = 1e-4

interface ResolvedLayout {
  size_ratio: number
  position_x: number
  position_y: number
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min
  return Math.min(max, Math.max(min, value))
}

function normalizeLayout(candidate: ResolvedLayout): ResolvedLayout {
  return {
    size_ratio: clamp(candidate.size_ratio, MIN_SIZE, MAX_SIZE),
    position_x: clamp(candidate.position_x, MIN_X, MAX_X),
    position_y: clamp(candidate.position_y, MIN_Y, MAX_Y),
  }
}

function resolveDefaults(
  style: CaptionStyle | undefined,
  overrides: CaptionOverrides | null | undefined,
): ResolvedLayout {
  const presetSize = style?.preview.sizeRatio ?? 0.05
  const presetX = style?.preview.positionX ?? 0.5
  const presetY =
    style?.preview.positionY ?? 1 - (style?.preview.marginRatio ?? 0.12)

  return normalizeLayout({
    size_ratio: overrides?.size_ratio ?? presetSize,
    position_x: overrides?.position_x ?? presetX,
    position_y: overrides?.position_y ?? presetY,
  })
}

function sameLayout(a: ResolvedLayout, b: ResolvedLayout): boolean {
  return (
    Math.abs(a.size_ratio - b.size_ratio) <= EPSILON &&
    Math.abs(a.position_x - b.position_x) <= EPSILON &&
    Math.abs(a.position_y - b.position_y) <= EPSILON
  )
}

function hasSavedOverrides(overrides: CaptionOverrides | null | undefined): boolean {
  if (!overrides) return false
  return (
    Number.isFinite(overrides.size_ratio) ||
    Number.isFinite(overrides.position_x) ||
    Number.isFinite(overrides.position_y)
  )
}

export function CaptionLayoutEditor({
  clipId,
  style,
  overrides,
  disabled,
  onCommit,
  onDirtyChange,
}: {
  clipId: string
  style: CaptionStyle | undefined
  overrides: CaptionOverrides | null | undefined
  disabled: boolean
  onCommit: (overrides: CaptionOverrides | null) => Promise<boolean>
  onDirtyChange: (dirty: boolean) => void
}) {
  const savedLayout = useMemo(
    () => resolveDefaults(style, overrides),
    [
      style?.key,
      style?.preview.sizeRatio,
      style?.preview.marginRatio,
      style?.preview.positionX,
      style?.preview.positionY,
      overrides?.size_ratio,
      overrides?.position_x,
      overrides?.position_y,
    ],
  )

  const [draft, setDraft] = useState<ResolvedLayout>(savedLayout)
  const draftRef = useRef<ResolvedLayout>(savedLayout)
  const savedRef = useRef<ResolvedLayout>(savedLayout)
  const clipIdRef = useRef<string>(clipId)
  const [committing, setCommitting] = useState(false)

  savedRef.current = savedLayout
  draftRef.current = draft

  useEffect(() => {
    const switchedClip = clipIdRef.current !== clipId
    clipIdRef.current = clipId
    if (switchedClip || sameLayout(draftRef.current, savedRef.current)) {
      draftRef.current = savedLayout
      setDraft(savedLayout)
    }
  }, [clipId, savedLayout])

  const updateDraft = useCallback(
    (updater: (current: ResolvedLayout) => ResolvedLayout) => {
      const next = normalizeLayout(updater(draftRef.current))
      draftRef.current = next
      setDraft(next)
    },
    [],
  )

  const isDirty = !sameLayout(draft, savedLayout)
  const canReset = hasSavedOverrides(overrides) || isDirty
  const locked = disabled || committing

  useEffect(() => {
    onDirtyChange(isDirty)
  }, [isDirty, onDirtyChange])

  const handleApply = async () => {
    if (locked || !isDirty) return
    const payload = normalizeLayout(draftRef.current)
    draftRef.current = payload
    setDraft(payload)
    setCommitting(true)
    try {
      await onCommit(payload)
    } finally {
      setCommitting(false)
    }
  }

  const handleDiscard = () => {
    if (locked || !isDirty) return
    draftRef.current = savedLayout
    setDraft(savedLayout)
  }

  const handleReset = async () => {
    if (locked || !canReset) return
    const presetDefault = resolveDefaults(style, null)
    if (!hasSavedOverrides(overrides)) {
      draftRef.current = presetDefault
      setDraft(presetDefault)
      return
    }
    setCommitting(true)
    try {
      const ok = await onCommit(null)
      if (ok) {
        draftRef.current = presetDefault
        setDraft(presetDefault)
      }
    } finally {
      setCommitting(false)
    }
  }

  return (
    <fieldset
      disabled={locked}
      className="space-y-3 border border-ink-800 bg-ink-900/40 p-3"
      aria-label="Caption layout"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <span className="eyebrow">Caption layout</span>
          <p className="mt-0.5 text-xs text-ink-400">
            Apply to save and preview. Preview is approximate; export uses saved layout.
          </p>
        </div>
        {isDirty && (
          <span className="text-xs font-medium text-sodium-500" role="status">
            Unsaved caption layout
          </span>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <label className="block text-xs text-ink-300">
          <span className="flex justify-between">
            <span>Caption size</span>
            <span className="numeric text-ink-400">{(draft.size_ratio * 100).toFixed(1)}%</span>
          </span>
          <input
            type="range"
            aria-label="Caption size"
            min={2}
            max={10}
            step={0.1}
            value={draft.size_ratio * 100}
            onChange={(e) => {
              const pct = Number(e.target.value)
              updateDraft((current) => ({ ...current, size_ratio: pct / 100 }))
            }}
            className="mt-1 w-full accent-sodium-500"
          />
        </label>

        <label className="block text-xs text-ink-300">
          <span className="flex justify-between">
            <span>Horizontal</span>
            <span className="numeric text-ink-400">{Math.round(draft.position_x * 100)}%</span>
          </span>
          <input
            type="range"
            aria-label="Caption horizontal position"
            min={10}
            max={90}
            step={1}
            value={Math.round(draft.position_x * 100)}
            onChange={(e) => {
              const pct = Number(e.target.value)
              updateDraft((current) => ({ ...current, position_x: pct / 100 }))
            }}
            className="mt-1 w-full accent-sodium-500"
          />
        </label>

        <label className="block text-xs text-ink-300">
          <span className="flex justify-between">
            <span>Vertical</span>
            <span className="numeric text-ink-400">{Math.round(draft.position_y * 100)}%</span>
          </span>
          <input
            type="range"
            aria-label="Caption vertical position"
            min={10}
            max={95}
            step={1}
            value={Math.round(draft.position_y * 100)}
            onChange={(e) => {
              const pct = Number(e.target.value)
              updateDraft((current) => ({ ...current, position_y: pct / 100 }))
            }}
            className="mt-1 w-full accent-sodium-500"
          />
        </label>
      </div>

      <div className="flex flex-wrap items-center justify-end gap-2 pt-1">
        <button
          type="button"
          disabled={locked || !canReset}
          onClick={() => void handleReset()}
          className="btn btn-ghost"
        >
          Reset caption layout
        </button>
        <button
          type="button"
          disabled={locked || !isDirty}
          onClick={handleDiscard}
          className="btn btn-ghost"
        >
          Discard caption layout
        </button>
        <button
          type="button"
          disabled={locked || !isDirty}
          onClick={() => void handleApply()}
          className="btn btn-primary"
        >
          {committing ? 'Applying…' : 'Apply caption layout'}
        </button>
      </div>
    </fieldset>
  )
}
