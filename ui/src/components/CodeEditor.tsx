import CodeMirror from '@uiw/react-codemirror'
import { yaml } from '@codemirror/lang-yaml'
import { markdown } from '@codemirror/lang-markdown'
import type { Extension } from '@codemirror/state'

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
  const extensions: Extension[] = format === 'markdown' ? [markdown()] : [yaml()]
  return (
    <CodeMirror
      value={value}
      onChange={onChange}
      theme="dark"
      height={height}
      readOnly={readOnly}
      extensions={extensions}
      basicSetup={{ lineNumbers: true, foldGutter: true, highlightActiveLine: !readOnly }}
      className="h-full text-sm"
    />
  )
}
