/**
 * Typed client for the AutoClip API.
 *
 * Same-origin throughout: the server hands out the built bundle, so there is no
 * base URL to configure and no CORS to negotiate.
 */

export interface Source {
  id: string
  type: string
  title: string
  url: string | null
  filename: string | null
  channel: string | null
  library_path?: string | null
  duration_s: number
  width: number | null
  height: number | null
  fps: number | null
  has_audio: boolean
  has_video: boolean
  created_at: string
}

export interface LibraryEntry {
  path: string
  name: string
  is_dir: boolean
  size_bytes: number | null
}

export interface LibraryListing {
  available: boolean
  directory: string
  entries: LibraryEntry[]
  truncated: boolean
}

export type JobStatus = 'queued' | 'running' | 'failed' | 'done' | 'cancelled'

export interface Job {
  id: string
  source_id: string
  status: JobStatus
  current_stage: string
  progress: number
  error: string | null
  provider: string
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
  source: Source | null
}

export interface ExportRecord {
  id: string
  clip_id: string
  ratio: string
  style: string
  size_bytes: number
  created_at: string
  download_url: string
}

export type ClipStatus = 'candidate' | 'kept' | 'discarded' | 'exported'

export interface AutoFraming {
  mode: 'auto'
}

export interface Framing {
  mode: 'fit' | 'split'
}

export interface ManualFraming {
  mode: 'manual'
  center_x: number
  center_y: number
  zoom: number
}

export type SingleFraming = AutoFraming | Framing | ManualFraming

export interface FramingSlice {
  start_s: number
  end_s: number
  framing: SingleFraming
}

export interface SlicesFraming {
  mode: 'slices'
  segments: FramingSlice[]
}

export type ClipFraming = AutoFraming | Framing | ManualFraming | SlicesFraming | null

export interface CaptionOverrides {
  size_ratio?: number
  position_x?: number
  position_y?: number
}

export interface Clip {
  id: string
  job_id: string
  rank: number
  start_s: number
  end_s: number
  duration_s: number
  start_word: number
  end_word: number
  title: string
  hook: string
  score: number
  reason: string
  status: ClipStatus
  user_trimmed: boolean
  caption_style: string
  ratio: string
  burn_captions?: boolean
  framing?: ClipFraming
  caption_overrides?: CaptionOverrides | null
  exports: ExportRecord[]
}

export interface ClipEditPatch {
  words?: Word[]
  caption_style?: string
  ratio?: string
  burn_captions?: boolean
  framing?: ClipFraming
  caption_overrides?: CaptionOverrides | null
}

export interface ClipPreviewRequest {
  start_s?: number
  duration_s?: number
}

export interface Word {
  text: string
  start: number
  end: number
  speaker: string | null
}

export interface CropKeyframe {
  t: number
  x: number
  y: number
}

export interface CropSegment {
  start_s: number
  end_s: number
  width: number
  height: number
  keyframes: CropKeyframe[]
  strategy: 'track' | 'wide' | 'general'
  zoom: number
  fit: boolean
  split?: boolean
}

/** Mirrors autoclip.pipeline.reframe.croppath.CropPath. */
export interface CropPath {
  source_width: number
  source_height: number
  segments: CropSegment[]
}

export interface CaptionStyle {
  key: string
  label: string
  description: string
  preview: {
    font: string
    primary: string
    accent: string | null
    outline: string
    outlineWidth: number
    allCaps: boolean
    sizeRatio: number
    marginRatio: number
    positionX?: number
    positionY?: number
    boxed: boolean
    animation: string
    maxWords: number
  }
}

export interface ProviderStatus {
  name: string
  available: boolean
  detail: string
  models: string[]
  requires_key: boolean
  has_key: boolean
}

export interface Settings {
  active_provider: string
  providers: Record<string, { model: string; base_url: string | null }>
  whisper: { model: string; compute_type: string; language: string; diarization: boolean }
  clips: { min_duration_s: number; max_duration_s: number; max_clips: number }
  ingest: {
    ytdlp_format: string
    cookies_from_browser: string
    prefer_youtube_captions: boolean
  }
  export: {
    ratio: string
    caption_style: string
    loudness_lufs: number
    prefer_hardware_encoder: boolean
    crf: number
    write_srt: boolean
  }
  insecure_secret_storage: boolean
  keys_present: Record<string, boolean>
}

export interface SystemStatus {
  ready: boolean
  python_version: string
  platform: string
  ffmpeg_version: string | null
  has_libass: boolean
  nvenc_works: boolean
  accel: string
  gpu_name: string | null
  compute_type: string
  diarization_available: boolean
}

export interface JobSettingsOverrides {
  provider?: string
  whisper_model?: string
  language?: string
  diarization?: boolean
  min_duration_s?: number
  max_duration_s?: number
  max_clips?: number
  caption_style?: string
  ratio?: string
}

export interface UploadProgress {
  loaded: number
  total: number
  lengthComputable: boolean
  uploadComplete?: boolean
}

export interface UploadOptions {
  onProgress?: (progress: UploadProgress) => void
  signal?: AbortSignal
}

/** An API error carrying the server's message and its actionable hint. */
export class ApiError extends Error {
  hint: string
  status: number

  constructor(message: string, status: number, hint = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.hint = hint
  }
}

function parseErrorBody(status: number, statusText: string, rawText: string): ApiError {
  let message = `${status} ${statusText}`.trim() || `HTTP ${status}`
  let hint = ''
  try {
    const body = JSON.parse(rawText)
    const detail = body?.detail
    if (typeof detail === 'string') {
      message = detail
    } else if (detail && typeof detail === 'object') {
      message = detail.message ?? message
      hint = detail.hint ?? ''
    }
  } catch {
    /* Response had no JSON body; the status line is the best we have. */
  }
  return new ApiError(message, status, hint)
}

function makeAbortError(): DOMException {
  return new DOMException('The operation was aborted.', 'AbortError')
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    const rawText = await response.text().catch(() => '')
    throw parseErrorBody(response.status, response.statusText, rawText)
  }

  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<{ status: string; version: string }>('/api/health'),
  system: () => request<SystemStatus>('/api/system'),
  fetchModels: () => request<void>('/api/system/models', { method: 'POST' }),

  listSources: () => request<Source[]>('/api/sources'),

  listLibrary: (directory = '') => {
    const query = directory ? `?directory=${encodeURIComponent(directory)}` : ''
    return request<LibraryListing>(`/api/library${query}`)
  },

  importLibrary: (path: string) =>
    request<Source>('/api/library/import', {
      method: 'POST',
      body: JSON.stringify({ path }),
    }),

  ingestYouTube: (url: string, cookiesFromBrowser?: string) =>
    request<Source>('/api/sources/youtube', {
      method: 'POST',
      body: JSON.stringify({ url, cookies_from_browser: cookiesFromBrowser || null }),
    }),

  uploadSource: (file: File, options: UploadOptions = {}): Promise<Source> => {
    const { onProgress, signal } = options

    return new Promise<Source>((resolve, reject) => {
      if (signal?.aborted) {
        reject(makeAbortError())
        return
      }

      const xhr = new XMLHttpRequest()
      let settled = false
      let lastLoaded = 0
      let lastTotal = file.size
      let lastComputable = file.size > 0

      const cleanup = () => {
        xhr.upload.removeEventListener('progress', handleProgress)
        xhr.upload.removeEventListener('load', handleUploadLoad)
        xhr.removeEventListener('load', handleLoad)
        xhr.removeEventListener('error', handleError)
        xhr.removeEventListener('abort', handleAbort)
        if (signal) {
          signal.removeEventListener('abort', handleSignalAbort)
        }
      }

      const settle = (fn: () => void) => {
        if (settled) return
        settled = true
        cleanup()
        fn()
      }

      const handleProgress = (event: ProgressEvent<XMLHttpRequestEventTarget>) => {
        if (settled) return
        lastLoaded = event.loaded
        lastTotal = event.lengthComputable ? event.total : 0
        lastComputable = event.lengthComputable
        onProgress?.({
          loaded: event.loaded,
          total: event.lengthComputable ? event.total : 0,
          lengthComputable: event.lengthComputable,
          uploadComplete: false,
        })
      }

      const handleUploadLoad = () => {
        if (settled) return
        const total = lastComputable && lastTotal > 0 ? lastTotal : lastLoaded
        onProgress?.({
          loaded: total,
          total,
          lengthComputable: lastComputable,
          uploadComplete: true,
        })
      }

      const handleLoad = () => {
        settle(() => {
          if (signal?.aborted) {
            reject(makeAbortError())
            return
          }
          if (xhr.status >= 200 && xhr.status < 300) {
            try {
              const parsed = JSON.parse(xhr.responseText) as Source
              resolve(parsed)
            } catch (err) {
              reject(
                err instanceof Error
                  ? err
                  : new SyntaxError('Malformed JSON response from /api/sources/upload'),
              )
            }
          } else {
            reject(parseErrorBody(xhr.status, xhr.statusText, xhr.responseText ?? ''))
          }
        })
      }

      const handleError = () => {
        settle(() => {
          reject(new TypeError('Network request failed'))
        })
      }

      const handleAbort = () => {
        settle(() => {
          reject(makeAbortError())
        })
      }

      const handleSignalAbort = () => {
        xhr.abort()
        settle(() => {
          reject(makeAbortError())
        })
      }

      xhr.open('POST', '/api/sources/upload')
      xhr.upload.addEventListener('progress', handleProgress)
      xhr.upload.addEventListener('load', handleUploadLoad)
      xhr.addEventListener('load', handleLoad)
      xhr.addEventListener('error', handleError)
      xhr.addEventListener('abort', handleAbort)
      if (signal) {
        signal.addEventListener('abort', handleSignalAbort, { once: true })
      }

      try {
        const form = new FormData()
        form.append('file', file)
        xhr.send(form)
      } catch (err) {
        settle(() => reject(err))
      }
    })
  },

  listJobs: (limit = 25) => request<Job[]>(`/api/jobs?limit=${limit}`),
  getJob: (id: string) => request<Job>(`/api/jobs/${id}`),

  createJob: (sourceId: string, settings: JobSettingsOverrides = {}) =>
    request<Job>('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({ source_id: sourceId, settings }),
    }),

  cancelJob: (id: string) => request<Job>(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  retryJob: (id: string) => request<Job>(`/api/jobs/${id}/retry`, { method: 'POST' }),

  listClips: (jobId: string) => request<Clip[]>(`/api/jobs/${jobId}/clips`),
  getClip: (clipId: string) => request<Clip>(`/api/clips/${clipId}`),

  getCropPath: (clipId: string) => request<CropPath>(`/api/clips/${clipId}/crop-path`),
  getClipWords: (clipId: string) => request<Word[]>(`/api/clips/${clipId}/words`),

  patchClip: (
    clipId: string,
    patch: { start_s?: number; end_s?: number; title?: string; status?: ClipStatus },
  ) => request<Clip>(`/api/clips/${clipId}`, { method: 'PATCH', body: JSON.stringify(patch) }),

  patchCaptions: (
    clipId: string,
    patch: { words?: Word[]; caption_style?: string; ratio?: string },
  ) =>
    request<Clip>(`/api/clips/${clipId}/captions`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  patchEdit: (clipId: string, patch: ClipEditPatch) =>
    request<Clip>(`/api/clips/${clipId}/edit`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  previewClip: async (
    clipId: string,
    params: ClipPreviewRequest = {},
    signal?: AbortSignal,
  ): Promise<Blob> => {
    const response = await fetch(`/api/clips/${clipId}/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start_s: params.start_s ?? 0, duration_s: params.duration_s ?? 3 }),
      signal,
    })
    if (!response.ok) {
      const rawText = await response.text().catch(() => '')
      throw parseErrorBody(response.status, response.statusText, rawText)
    }
    return response.blob()
  },

  exportClip: (
    clipId: string,
    ratio?: string,
    style?: string,
    writeSrt?: boolean,
  ) => {
    const body: { ratio?: string; style?: string; write_srt?: boolean } = {}
    if (ratio !== undefined) body.ratio = ratio
    if (style !== undefined) body.style = style
    if (writeSrt !== undefined) body.write_srt = writeSrt
    return request<ExportRecord>(`/api/clips/${clipId}/export`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },

  captionStyles: () => request<CaptionStyle[]>('/api/caption-styles'),
  providerStatus: () => request<ProviderStatus[]>('/api/providers/status'),

  getSettings: () => request<Settings>('/api/settings'),
  putSettings: (patch: Partial<Settings>) =>
    request<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(patch) }),

  putSecret: (key: string, value: string) =>
    request<void>('/api/settings/secrets', {
      method: 'PUT',
      body: JSON.stringify({ key, value }),
    }),

  deleteSecret: (key: string) =>
    request<void>(`/api/settings/secrets/${key}`, { method: 'DELETE' }),

  mediaUrl: (jobId: string) => `/api/jobs/${jobId}/media`,
}

/** Format seconds as m:ss, or h:mm:ss past an hour. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const s = total % 60
  const m = Math.floor(total / 60) % 60
  const h = Math.floor(total / 3600)
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`
}

/** Format seconds as timecode with centiseconds, for trim handles. */
export function formatTimecode(seconds: number): string {
  const total = Math.max(0, seconds)
  const cs = Math.floor((total % 1) * 100)
  const s = Math.floor(total) % 60
  const m = Math.floor(total / 60)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(m)}:${pad(s)}.${pad(cs)}`
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}
