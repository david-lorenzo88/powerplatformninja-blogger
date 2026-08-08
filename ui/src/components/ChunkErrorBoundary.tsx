import { Component } from 'react'
import type { ReactNode } from 'react'
import { forceReload, reloadOnce } from '../lib/chunkReload'

// A tab left open across a deploy is holding route-chunk names that no longer
// exist on the single replica: the next lazy import 404s, the promise rejects,
// and React unmounts the whole tree to a blank screen. There is no recovery but
// a reload.
//
// Anything that is not a chunk-load failure is re-thrown. Swallowing real render
// errors behind a reload would make them impossible to find.
const CHUNK_ERROR =
  /dynamically imported module|Importing a module script failed|error loading dynamically imported|ChunkLoadError|Failed to fetch/i

export class ChunkErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError(error: Error): { failed: boolean } {
    if (CHUNK_ERROR.test(error.message)) return { failed: true }
    throw error
  }

  componentDidCatch(error: Error): void {
    // If the cooldown refused the reload we stay put and show the message
    // below; otherwise the page is already on its way out.
    if (CHUNK_ERROR.test(error.message)) reloadOnce()
  }

  render(): ReactNode {
    if (this.state.failed) {
      // Only reached when the automatic reload already happened and did not
      // help, so say so rather than spinning.
      return (
        <div className="p-6 text-sm text-slate-400">
          <p>This screen could not be loaded.</p>
          <p className="mt-1 text-xs text-slate-500">
            Its code could not be fetched — usually a deploy that landed while this tab was
            open, or no connection.
          </p>
          <button onClick={forceReload} className="mt-3 min-h-11 text-accent hover:underline">
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
