import { useCallback, useEffect, useRef, useState } from 'react'

import { api, formatBytes, type LibraryListing } from '../api'

interface LibraryPickerProps {
  disabled: boolean
  busy?: boolean
  onSelect: (path: string) => void
}

function parentDirectory(dir: string): string {
  const normalized = dir.replace(/\/+$/, '')
  const idx = normalized.lastIndexOf('/')
  return idx === -1 ? '' : normalized.slice(0, idx)
}

export function LibraryPicker({ disabled, busy = false, onSelect }: LibraryPickerProps) {
  const [directory, setDirectory] = useState('')
  const [listing, setListing] = useState<LibraryListing | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedFile, setSelectedFile] = useState('')
  const requestIdRef = useRef(0)

  const loadDirectory = useCallback((targetDir: string) => {
    const reqId = ++requestIdRef.current
    setLoading(true)
    setError(null)

    api
      .listLibrary(targetDir)
      .then((res) => {
        if (requestIdRef.current !== reqId) return
        setListing(res)
        setDirectory(res.directory || '')
        const firstFile = res.entries.find((e) => !e.is_dir)
        setSelectedFile((prev) =>
          prev && res.entries.some((e) => !e.is_dir && e.path === prev)
            ? prev
            : (firstFile?.path ?? ''),
        )
      })
      .catch((err) => {
        if (requestIdRef.current !== reqId) return
        setError(err instanceof Error ? err.message : 'Failed to load server media folder')
      })
      .finally(() => {
        if (requestIdRef.current !== reqId) return
        setLoading(false)
      })
  }, [])

  useEffect(() => {
    loadDirectory('')
    return () => {
      requestIdRef.current += 1
    }
  }, [loadDirectory])

  const navigateTo = (nextDir: string) => {
    if (disabled || loading) return
    setSelectedFile('')
    setDirectory(nextDir)
    loadDirectory(nextDir)
  }

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    if (disabled || loading || !selectedFile) return
    onSelect(selectedFile)
  }

  const controlsDisabled = disabled || loading
  const currentDir = directory
  const directories = listing ? listing.entries.filter((e) => e.is_dir) : []
  const files = listing ? listing.entries.filter((e) => !e.is_dir) : []

  return (
    <div className="mt-8 border-t border-ink-850 pt-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="eyebrow">Server media library</span>
        {listing?.available && (
          <span
            className="max-w-full truncate font-mono text-xs text-ink-400"
            title={currentDir ? `/${currentDir}` : '/'}
          >
            /{currentDir}
          </span>
        )}
      </div>

      {loading && !listing ? (
        <p role="status" className="mt-3 text-xs text-ink-500">
          Loading server media folder…
        </p>
      ) : error ? (
        <div role="alert" className="mt-3 flex flex-wrap items-center gap-3 text-xs text-signal-bad">
          <span>Could not load server media folder ({error}).</span>
          <button
            type="button"
            onClick={() => loadDirectory(currentDir)}
            disabled={controlsDisabled}
            className="btn btn-quiet text-xs"
          >
            Retry
          </button>
        </div>
      ) : !listing?.available ? (
        <div className="mt-3">
          <p className="text-xs text-ink-400">No server media folder available</p>
          <p className="mt-1 text-xs text-ink-500">
            Place files in the server media folder to browse and process them directly without uploading.
          </p>
        </div>
      ) : (
        <div className="mt-3 space-y-4">
          {(currentDir !== '' || directories.length > 0) && (
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-ink-500">Folders</span>
                {currentDir !== '' && (
                  <button
                    type="button"
                    onClick={() => navigateTo(parentDirectory(currentDir))}
                    disabled={controlsDisabled}
                    className="btn btn-quiet text-xs"
                  >
                    ← Back
                  </button>
                )}
              </div>

              {directories.length > 0 && (
                <ul className="flex flex-wrap gap-1.5" aria-label="Subfolders">
                  {directories.map((dir) => (
                    <li key={dir.path} className="max-w-full">
                      <button
                        type="button"
                        onClick={() => navigateTo(dir.path)}
                        disabled={controlsDisabled}
                        title={dir.name}
                        className="max-w-full truncate border border-ink-800 bg-ink-850/60 px-2.5 py-1 text-left text-xs text-ink-200 transition-colors hover:border-ink-600 hover:text-ink-100 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {dir.name}/
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {loading ? (
            <p role="status" className="text-xs text-ink-500">
              Loading folder…
            </p>
          ) : files.length === 0 ? (
            <p className="text-xs text-ink-500">
              {directories.length === 0
                ? 'This folder is empty.'
                : 'No media files in this folder.'}
            </p>
          ) : (
            <form onSubmit={handleSubmit}>
              <label htmlFor="library-file-select" className="sr-only">
                Server media file
              </label>
              <div className="flex flex-wrap items-end gap-4">
                <select
                  id="library-file-select"
                  value={selectedFile}
                  onChange={(e) => setSelectedFile(e.target.value)}
                  disabled={controlsDisabled}
                  className="field min-w-0 flex-1 cursor-pointer text-sm"
                >
                  {files.map((file) => (
                    <option key={file.path} value={file.path} className="bg-ink-850">
                      {file.name}
                      {file.size_bytes !== null ? ` (${formatBytes(file.size_bytes)})` : ''}
                    </option>
                  ))}
                </select>
                <button
                  type="submit"
                  disabled={controlsDisabled || !selectedFile}
                  className="btn btn-primary shrink-0"
                >
                  {busy ? 'Starting…' : 'Process file'}
                </button>
              </div>
            </form>
          )}

          {listing.truncated && (
            <p className="text-xs text-ink-500">
              Showing first {listing.entries.length} items. Organize files into subfolders to browse everything.
            </p>
          )}

          <p className="text-xs leading-relaxed text-ink-500">
            Uses file in place. Keep it available while processing and editing.
          </p>
        </div>
      )}
    </div>
  )
}
