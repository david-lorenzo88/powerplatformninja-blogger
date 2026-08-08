import CodeMirror, { EditorView } from '@uiw/react-codemirror'
import { yaml } from '@codemirror/lang-yaml'
import { markdown } from '@codemirror/lang-markdown'
import type { Extension } from '@codemirror/state'
import { useIsDesktop } from '../hooks/useMediaQuery'

// One place to wire CodeMirror so the Config (YAML) and Drafts (Markdown) editors
// share a theme and behaviour. `format` picks the language for highlighting.
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
  const extensions: Extension[] = format === 'markdown' ? [markdown()] : [yaml()]
  // Sideways scrolling to finish a line is miserable with a thumb, and a wrapped
  // long string is still perfectly readable YAML. Desktop keeps hard lines,
  // where the horizontal room exists and column alignment is worth having.
  if (!isDesktop) extensions.push(EditorView.lineWrapping)
  return (
    <CodeMirror
      value={value}
      onChange={onChange}
      theme="dark"
      height={height}
      readOnly={readOnly}
      extensions={extensions}
      // The line-number and fold gutters cost about 48px, which is an eighth of
      // a 390px screen given to chrome rather than to the YAML being read.
      basicSetup={{
        lineNumbers: isDesktop,
        foldGutter: isDesktop,
        highlightActiveLine: !readOnly,
      }}
      className="h-full text-sm"
    />
  )
}
