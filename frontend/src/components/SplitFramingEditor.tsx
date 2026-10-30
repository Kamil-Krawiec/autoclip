import { useEffect, useRef, useState } from 'react'
import type { SplitFraming, SplitPanel } from '../api'
import type { PanRequest } from './ClipPlayer'

export type SplitPanels = [SplitPanel, SplitPanel]
const legacyPanels: SplitPanels = [
  { x: 0, y: 0, width: 0.5, height: 1 },
  { x: 0.5, y: 0, width: 0.5, height: 1 },
]

export function SplitFramingEditor({ framing, disabled, onCommit, onDirtyChange, onDraftSplit, panRequest }: {
  framing: SplitFraming
  disabled: boolean
  onCommit: (framing: SplitFraming) => Promise<boolean>
  onDirtyChange: (dirty: boolean) => void
  onDraftSplit: (panels: SplitPanels | null) => void
  panRequest: PanRequest | null
}) {
  const saved = framing.panels ?? legacyPanels
  const [draft, setDraft] = useState<SplitPanels>(saved)
  const [editing, setEditing] = useState(false)
  const [committing, setCommitting] = useState(false)
  const lastRequest = useRef(panRequest)
  const dirty = editing && JSON.stringify(draft) !== JSON.stringify(saved)
  const locked = disabled || committing

  useEffect(() => { setDraft(saved) }, [JSON.stringify(saved)])
  useEffect(() => { onDraftSplit(editing ? draft : null) }, [editing, draft, onDraftSplit])
  useEffect(() => () => onDraftSplit(null), [onDraftSplit])
  useEffect(() => { onDirtyChange(dirty) }, [dirty, onDirtyChange])
  useEffect(() => {
    if (lastRequest.current === panRequest) return
    lastRequest.current = panRequest
    if (!editing || locked || !panRequest || panRequest.panelIndex === undefined) return
    const { panelIndex, dx, dy, resize } = panRequest
    setDraft(current => current.map((panel, index) => {
      if (index !== panelIndex) return panel
      if (resize) return {
        ...panel,
        width: Math.min(1 - panel.x, Math.max(0.02, panel.width + dx)),
        height: Math.min(1 - panel.y, Math.max(0.02, panel.height + dy)),
      }
      return {
        ...panel,
        x: Math.max(0, Math.min(1 - panel.width, panel.x + dx)),
        y: Math.max(0, Math.min(1 - panel.height, panel.y + dy)),
      }
    }) as SplitPanels)
  }, [panRequest, editing, locked])

  const close = () => { setDraft(saved); setEditing(false) }
  const apply = async () => {
    if (!dirty || locked) return
    setCommitting(true)
    try { if (await onCommit({ mode: 'split', panels: draft })) setEditing(false) }
    finally { setCommitting(false) }
  }
  return <fieldset disabled={locked} className="space-y-3 border border-ink-800 p-3" aria-label="Split framing editor">
    <p className="text-xs text-ink-400">Top and bottom panels keep their proportions and fit inside the output.</p>
    {editing ? <>
      <p className="text-xs text-ink-400">Move either frame or drag its corner to resize. Arrow keys move; corner arrow keys resize. Shift moves faster.</p>
      {dirty && <span role="status" className="text-xs text-sodium-500">Unsaved split framing</span>}
      <div className="flex flex-wrap justify-end gap-2">
        <button type="button" className="btn btn-ghost" onClick={close}>Discard split changes</button>
        <button type="button" className="btn btn-primary" disabled={!dirty || locked} onClick={() => void apply()}>{committing ? 'Applying…' : 'Apply split framing'}</button>
      </div>
    </> : <button type="button" className="btn btn-ghost" onClick={() => { setDraft(saved); setEditing(true) }}>Edit split frames</button>}
  </fieldset>
}

export function SplitFrameOverlay({ panels, onTransform, disabled }: {
  panels: SplitPanels
  onTransform: (index: number, dx: number, dy: number, resize: boolean) => void
  disabled: boolean
}) {
  const [activePanel, setActivePanel] = useState(0)
  const drag = useRef<{ pointer: number; index: number; x: number; y: number; resize: boolean } | null>(null)
  const start = (event: React.PointerEvent<HTMLElement>, index: number, resize: boolean) => {
    event.stopPropagation()
    if (disabled || event.button !== 0) return
    setActivePanel(index)
    drag.current = { pointer: event.pointerId, index, x: event.clientX, y: event.clientY, resize }
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const move = (event: React.PointerEvent<HTMLElement>) => {
    event.stopPropagation()
    const current = drag.current
    if (!current || current.pointer !== event.pointerId || disabled) return
    const bounds = event.currentTarget.closest('[data-testid="clip-preview"]')?.getBoundingClientRect()
    if (!bounds?.width || !bounds.height) return
    onTransform(current.index, (event.clientX - current.x) / bounds.width, (event.clientY - current.y) / bounds.height, current.resize)
    current.x = event.clientX; current.y = event.clientY
  }
  const finish = (event: React.PointerEvent<HTMLElement>) => { event.stopPropagation(); drag.current = null }
  const keyboard = (event: React.KeyboardEvent<HTMLElement>, index: number, resize: boolean) => {
    event.stopPropagation()
    const directions: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }
    const delta = directions[event.key]
    if (!delta || disabled) return
    event.preventDefault()
    const step = event.shiftKey ? 0.02 : 0.005
    onTransform(index, delta[0] * step, delta[1] * step, resize)
  }
  return <>
    <div className="absolute left-1/2 top-1 z-50 flex -translate-x-1/2 gap-1">
      {[0, 1].map(index => <button key={index} type="button" className="rounded bg-black/80 px-2 py-1 text-xs text-white" disabled={disabled} aria-pressed={activePanel === index} onClick={event => { event.stopPropagation(); setActivePanel(index) }}>Select {index === 0 ? 'top' : 'bottom'} frame</button>)}
    </div>
    {panels.map((panel, index) => <div key={index}
    data-testid={`split-frame-${index}`} role="group" tabIndex={disabled ? -1 : 0}
    aria-label={`${index === 0 ? 'Top' : 'Bottom'} split frame. Arrow keys move.`}
    className={`absolute cursor-move border-2 ${index === 0 ? 'border-orange-500 bg-orange-400/10' : 'border-sky-400 bg-sky-400/10'}`}
    onFocus={() => setActivePanel(index)}
    style={{ zIndex: activePanel === index ? 32 : 30, left: `${panel.x * 100}%`, top: `${panel.y * 100}%`, width: `${panel.width * 100}%`, height: `${panel.height * 100}%`, touchAction: 'none' }}
    onPointerDown={event => start(event, index, false)} onPointerMove={move} onPointerUp={finish} onPointerCancel={finish}
    onLostPointerCapture={finish} onKeyDown={event => keyboard(event, index, false)} onClick={event => event.stopPropagation()}>
    <span className="pointer-events-none absolute left-1 top-1 bg-black/70 px-1 text-xs text-white">{index === 0 ? 'Top' : 'Bottom'}</span>
    <button type="button" aria-label={`Resize ${index === 0 ? 'top' : 'bottom'} split frame`} disabled={disabled}
      className="absolute bottom-0 right-0 size-7 cursor-nwse-resize rounded-tl border-2 border-white bg-ink-900"
      onPointerDown={event => start(event, index, true)} onPointerMove={move} onPointerUp={finish} onPointerCancel={finish}
      onLostPointerCapture={finish} onKeyDown={event => keyboard(event, index, true)} onClick={event => event.stopPropagation()} />
  </div>)}</>
}
