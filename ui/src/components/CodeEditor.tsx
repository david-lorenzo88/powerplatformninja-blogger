import { useState } from 'react'
import CodeMirror, { EditorView } from '@uiw/react-codemirror'
import { yaml } from '@codemirror/lang-yaml'
import { markdown } from '@codemirror/lang-markdown'
import type { Extension } from '@codemirror/state'
import { useIsDesktop } from '../hooks/useMediaQuery'

// One place to wire CodeMirror so the Config (YAML) and Drafts (Markdown) editors
// share a theme and behaviour. `format` picks the language for highlighting.
//
// On a phone the editor opens read-only. CodeMirror under a touch keyboard is
// genuinely hostile — the selection handles fight the editor's own, and there is
// no autocorrect control — and `sources.yaml` is a file the whole crew reads on
// every run. Reading it from a phone is a real need; editing it by accident is a
// real risk. The toggle keeps it possible, but never the default.
export function CodeEditor({
  value,
  onChange,
  format,
  readOnly,
  height = '100%',
}: {
  value: string
  onChange?: (v: string) => void
  format: 'yaml' | 'markdown' | string
  readOnly?: boolean
  height?: string
}) {
  const isDesktop = useIsDesktop()
  const [unlocked, setUnlocked] = useState(false)
  // A caller that asked for read-only gets read-only, with no toggle offered.
  const lockedForTouch = !readOnly && !isDesktop && !unlocked

  const extensions: Extension[] = format === 'markdown' ? [markdown()] : [yaml()]
  // Sideways scrolling to finish a line is miserable with a thumb, and a wrapped
  // long string is still perfectly readable YAML. Desktop keeps hard lines,
  // where the horizontal room exists and column alignment is worth having.
  if (!isDesktop) extensions.push(EditorView.lineWrapping)

  return (
    <div className="flex h-full flex-col">
      {lockedForTouch && (
        <div className="flex shrink-0 items-center gap-2 border-b border-slate-800 bg-slate-900/60 px-4 py-1.5 text-xs text-slate-400">
          <span>Read-only on mobile</span>
          <button
            onClick={() => setUnlocked(true)}
            className="ml-auto min-h-11 px-2 font-medium text-accent active:underline"
          >
            Edit anyway
          </button>
        </div>
      )}
      <div className="min-h-0 flex-1">
        <CodeMirror
          value={value}
          onChange={onChange}
          theme="dark"
          height={height}
          readOnly={readOnly || lockedForTouch}
          extensions={extensions}
          // The line-number and fold gutters cost about 48px, which is an eighth
          // of a 390px screen given to chrome rather than to the YAML being read.
          basicSetup={{
            lineNumbers: isDesktop,
            foldGutter: isDesktop,
            highlightActiveLine: !readOnly && !lockedForTouch,
          }}
          className="h-full text-sm"
        />
      </div>
    </div>
  )
}
