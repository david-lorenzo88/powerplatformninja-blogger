// One-off: rasterise the app icons from their flat SVG sources.
//
// The PNGs are committed rather than built. They are static brand assets that
// change roughly never, and sharp is a native binary — putting it in the image
// build would cost an `npm ci` compile on every deploy to produce bytes that are
// identical every time. Run this by hand if the logo changes:
//
//   npm install --no-save sharp && node scripts/gen-icons.mjs && npm rm sharp
//
// Sources live in ui/icons-src/ — build inputs, not shipped assets — and are
// generated from the bolt path in public/favicon.svg — which cannot be rasterised directly: it is a
// 48x46 viewBox (non-square, so it would distort), built from a mask plus 15
// feGaussianBlur primitives that bloom into mush at 512px, and filled with a
// display-p3 colour that rasterisers disagree about.
import sharp from 'sharp'
import { readFileSync } from 'node:fs'

const OUT = new URL('../public/', import.meta.url).pathname
const SRC = new URL('../icons-src/', import.meta.url).pathname

const jobs = [
  // Transparent, `purpose: any`.
  { from: 'icon.svg', to: 'icons/icon-192.png', size: 192 },
  { from: 'icon.svg', to: 'icons/icon-512.png', size: 512 },
  // Opaque: a maskable icon is cropped to whatever shape the launcher wants, so
  // it must paint its own background out to the edges. The bolt sits at 50% of
  // the canvas, well inside the central 80% safe circle.
  { from: 'maskable.svg', to: 'icons/maskable-512.png', size: 512 },
  // Opaque because iOS composites any transparency onto white, and 180px
  // because that is what it asks for. iOS rounds the corners itself.
  { from: 'apple-touch.svg', to: 'apple-touch-icon.png', size: 180 },
]

for (const { from, to, size } of jobs) {
  await sharp(readFileSync(SRC + from))
    .resize(size, size)
    .png({ compressionLevel: 9 })
    .toFile(OUT + to)
  console.log(`${to}  ${size}x${size}`)
}
