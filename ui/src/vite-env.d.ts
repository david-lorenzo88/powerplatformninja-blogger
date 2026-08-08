/// <reference types="vite/client" />

// Injected by `define` in vite.config.ts. The persisted query cache is keyed on
// it, so a deploy that changes a response shape cannot rehydrate yesterday's
// objects into today's components.
declare const __BUILD_ID__: string
